import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agentteams_qoder.core import common
from agentteams_qoder.native import NativeTurn, TASK_TOOL
from agentteams_qoder.sessions import SessionStore


CORE = Path(os.environ["AT_SOURCE"]).resolve()
PLUGIN = CORE / "plugins/teamharness"


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recovery, self.matrix = Mock(), Mock()
        self.recovery.tools_current.return_value = {"execution": {"checkpoint": None}}
        self.recovery.begin_tools.return_value = "operation"
        self.turn = NativeTurn(PLUGIN, {"role": "worker", "sourceEvent": "$source"},
                               recovery=self.recovery, matrix=self.matrix, kind="task", identity={"task_id": "t"})

    async def test_actual_sdk_registration_preserves_public_schema(self):
        from mcp.types import ListToolsRequest

        server = self.turn.server()["instance"]
        result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
        tools = result.root.tools
        self.assertEqual([tool.name for tool in tools], ["task"])
        self.assertEqual(tools[0].inputSchema, common(PLUGIN, "task_native").invoke({"operation": "schema", "role": "worker"})["inputSchema"])
        self.assertNotIn("create", tools[0].inputSchema["properties"]["action"]["enum"])

    async def test_core_does_not_shadow_sdk_mcp_package(self):
        import mcp

        self.assertNotEqual(Path(mcp.__file__).parent, PLUGIN / "mcp")
        self.assertTrue(common(PLUGIN, "task_context").task_write_path_allowed("a.txt", "/tmp/out", "/tmp/scratch"))

    async def test_leader_schema_and_question_registration_follow_the_admitted_phase(self):
        from mcp.types import ListToolsRequest
        self.turn.context["role"] = "leader"
        self.turn.context["planning"] = ["main"]
        self.turn.ask = Mock()
        server = self.turn.server()["instance"]
        result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
        self.assertEqual([tool.name for tool in result.root.tools], ["task"])
        actions = result.root.tools[0].inputSchema["properties"]["action"]["enum"]
        self.assertIn("plan", actions)
        self.assertIn("accept", actions)
        self.assertNotIn("submit", actions)

    async def test_original_tool_id_is_journaled_before_execution(self):
        with patch.dict(os.environ, {"AGENTTEAMS_CONTROLLER_URL": ""}):
            self.turn.before("Read", {"file_path": "/tmp/a"}, "native-123")
        self.recovery.begin_tools.assert_called_once_with({"task_id": "t"}, [
            {"id": "native-123", "name": "Read", "arguments": {"file_path": "/tmp/a"}, "read_only": True,
             "pre_effect_error_codes": ["QODER_PERMISSION_DENIED"]}])
        self.turn.after("Read", "native-123", "file content")
        self.recovery.finish_tools.assert_called_once_with({"task_id": "t"}, "operation", [
            {"id": "native-123", "content": "file content"}])

    async def test_mutating_tool_error_retains_unknown_operation(self):
        self.turn.operations["call"] = ("Bash", "operation")
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.turn.after("Bash", "call", "failed", failed=True)
        self.assertIn("call", self.turn.operations)
        self.recovery.finish_tools.assert_not_called()

    async def test_read_only_terminal_error_has_no_effect(self):
        self.turn.operations["call"] = ("Read", "operation")
        self.turn.after("Read", "call", "not found", failed=True)
        self.assertEqual(self.recovery.finish_tools.call_args.args[2][0]["effect"], "none")

    async def test_interrupted_read_remains_unknown(self):
        self.turn.operations["call"] = ("Read", "operation")
        with self.assertRaises(ValueError):
            self.turn.after("Read", "call", "cancelled", failed=True, interrupted=True)
        self.recovery.finish_tools.assert_not_called()

    async def test_unregistered_task_name_cannot_bypass_journal(self):
        with patch.dict(os.environ, {"AGENTTEAMS_CONTROLLER_URL": ""}):
            self.turn.before(TASK_TOOL, {"action": "get"}, "call")
        self.recovery.begin_tools.assert_called_once()

    async def test_registered_task_delegates_without_generic_journal(self):
        self.turn.server()
        with patch.dict(os.environ, {"AGENTTEAMS_CONTROLLER_URL": ""}):
            self.turn.before(TASK_TOOL, {"action": "get"}, "call")
        self.recovery.begin_tools.assert_not_called()

    async def test_review_rejects_non_task_tools(self):
        self.turn.context["review"] = ["t", "submission"]
        with self.assertRaisesRegex(ValueError, "only"):
            self.turn.before("Read", {}, "call")
        self.recovery.begin_tools.assert_not_called()

    async def test_file_escape_is_rejected_before_admission(self):
        self.turn.context.update(execution="t", context={"outputDirectory": "/tmp/tasks/t/workspace", "scratchDirectory": "/tmp/tasks/t/scratch"})
        for path in ("../other/file", "/etc/passwd"):
            with self.assertRaisesRegex(ValueError, "outputDirectory"):
                self.turn.before("Write", {"file_path": path}, "call")
        self.recovery.begin_tools.assert_not_called()

    async def test_background_commands_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Background"):
            self.turn.before("Bash", {"command": "sleep 10", "run_in_background": True}, "call")

    async def test_running_bash_handle_is_not_a_terminal_receipt(self):
        self.turn.operations["call"] = ("Bash", "operation")
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.turn.after("Bash", "call", {"kind": "background", "taskId": "native-task"})
        self.recovery.finish_tools.assert_not_called()

    async def test_native_completed_bash_receipt_is_saved(self):
        self.turn.operations["call"] = ("Bash", "operation")
        self.turn.after("Bash", "call", {"kind": "completed", "exitCode": 0, "interrupted": False, "stdout": "ok"})
        self.recovery.finish_tools.assert_called_once()

    async def test_unmatched_terminal_receipt_is_rejected(self):
        self.turn.operations["call"] = ("Read", "operation")
        with self.assertRaises(ValueError):
            self.turn.after("Write", "call", "ok")

    async def test_unknown_receipt_stops_native_loop(self):
        self.turn.operations["call"] = ("Bash", "operation")
        result = await self.turn.hook({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "error": "unknown"}, "call", None)
        self.assertIs(result["continue_"], False)

    async def test_native_permission_denial_closes_only_the_exact_pre_effect_operation(self):
        with patch.dict(os.environ, {"AGENTTEAMS_CONTROLLER_URL": ""}):
            self.turn.before("Bash", {"command": "echo fixture"}, "native-denied")
        result = await self.turn.hook({"hook_event_name": "PermissionDenied", "tool_name": "Bash",
            "tool_use_id": "native-denied", "denial_reason": "Human rejected"}, "native-denied", None)
        self.assertEqual(result, {})
        receipt = self.recovery.finish_tools.call_args.args[2][0]
        self.assertEqual((receipt["effect"], receipt["error_code"]), ("none", "QODER_PERMISSION_DENIED"))
        from task_operation import begin, result_bytes
        _, checkpoint = begin(None, "fixture", self.recovery.begin_tools.call_args.args[1])
        self.assertTrue(result_bytes(checkpoint, checkpoint["operation"]["id"], [receipt]))
        self.assertFalse(self.turn.operations)
        await self.turn.hook({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "error": "Denied"}, "native-denied", None)
        self.recovery.finish_tools.assert_called_once()

    async def test_mismatched_native_permission_denial_cannot_close_another_tool(self):
        self.turn.operations["other"] = ("Bash", "op")
        result = await self.turn.hook({"hook_event_name": "PermissionDenied", "tool_name": "Write"}, "other", None)
        self.assertFalse(result["continue_"])
        self.assertIn("other", self.turn.operations)
        self.recovery.finish_tools.assert_not_called()

    async def test_incomplete_scoped_turn_must_continue(self):
        self.turn.context["planning"] = ["main"]
        with patch.object(self.turn, "admit"):
            result = await self.turn.stop({}, None, None)
        self.assertEqual(result["decision"], "block")


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_rows_survive_reopen_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.sqlite"
            store = SessionStore(path)
            key = {"project_key": "project", "session_id": "session"}
            rows = [{"type": "user", "uuid": "a", "message": {"content": "one"}},
                    {"type": "assistant", "uuid": "b", "message": {"content": "two"}}]
            await store.append(key, rows)
            digest = store.digest("session")
            await store.append(key, rows)
            reopened = SessionStore(path)
            self.assertEqual(await reopened.load(key), rows)
            self.assertEqual(reopened.digest("session"), digest)
            with self.assertRaisesRegex(ValueError, "changed"):
                await reopened.append(key, [{"type": "user", "uuid": "a", "message": {"content": "changed"}}])

    async def test_native_commit_callback_follows_durable_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "native.sqlite")
            key = {"project_key": "project", "session_id": "session"}
            async def committed(key, rows):
                self.assertEqual(await SessionStore(store.path).load(key), rows)
            store.committed = committed
            await store.append(key, [{"type": "user", "uuid": "a"}])


if __name__ == "__main__":
    unittest.main()
