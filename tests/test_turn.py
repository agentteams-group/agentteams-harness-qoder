import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qoder_agent_sdk import AssistantMessage, ResultMessage, ThinkingBlock, UserMessage

from agentteams_qoder.runtime import Runtime
from agentteams_qoder.sessions import SessionStore
from test_config import record
import os


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, mode, *, tracing=False):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime.__new__(Runtime)
            runtime.home, runtime.plugin = Path(directory), Path(os.environ["AT_SOURCE"]) / "plugins/teamharness"
            runtime.config, runtime.role = record(), "worker"
            runtime.model = {"model": "fixture", "url": "https://gateway/v1", "api_key": "synthetic"}
            runtime.matrix, runtime.recovery, runtime.client = Mock(), Mock(), Mock()
            runtime.matrix.message_claim.return_value = {"recovery": "resume", "identity": {"event_key": "control"}, "execution": {"checkpoint": None}}
            runtime.matrix.message_begin_tools.return_value = "steer-operation"
            runtime.pending_requests, runtime.active, runtime.telemetry = {}, None, None
            if tracing:
                from opentelemetry.sdk.trace import TracerProvider
                runtime.telemetry = TracerProvider()
            runtime.store = SessionStore(Path(directory) / "sessions.sqlite")
            runtime.prepare = Mock(return_value=({"role": "worker"}, "original"))
            calls = []

            class Client:
                def __init__(self, *, options):
                    self.options = options
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *_args):
                    return False
                async def get_available_models(self):
                    return [{"value": "agentteams-gateway/fixture", "isEnabled": True}]
                async def get_mcp_status(self):
                    return {"mcpServers": [{"name": "agentteams", "tools": [{"name": "task"}]}]}
                async def interrupt(self):
                    calls.append("interrupt")
                async def query(self, text, *, message_uuid, priority="next"):
                    if tracing:
                        from opentelemetry.propagate import inject
                        runtime.fixture_carrier = {}
                        inject(runtime.fixture_carrier)
                    calls.append((text, message_uuid, priority))
                    await runtime.store.append({"project_key": "fixture", "session_id": self.options.session_id},
                        [{"type": "user", "uuid": message_uuid, "message": {"role": "user", "content": text}}] if len(calls) == 1 else
                        [{"type": "attachment", "uuid": "native-attachment", "attachment": {
                            "type": "queued_command", "source_uuid": message_uuid, "prompt": text, "isMeta": False}}])
                async def receive_messages(self):
                    yield AssistantMessage([ThinkingBlock("Synthetic reasoning", "")], "fixture", message_id="assistant")
                    if mode != "plain":
                        action = "stop" if mode.startswith("stop") else "steer"
                        item = {"key": "control", "event_id": "$supplement", "room_id": "!dm:test", "input": {
                            "sender": "@human:test", "content": {"body": "Supplement", "com.agentteams.control": {
                                "action": action, "agent_id": "@worker:test", "run_id": "$original"}}}}
                        if mode == "stop_unknown":
                            runtime.active["native"].operations["uncertain"] = ("Bash", "operation")
                        await runtime.control(item)
                        runtime.matrix.message_finish_tools.assert_not_called()
                        runtime.matrix.message_cancel.assert_not_called()
                    if mode.startswith("stop"):
                        yield ResultMessage("interrupted", 1, 1, True, 1, "fixture")
                        return
                    if mode != "plain":
                        yield ResultMessage("success", 1, 1, False, 1, "fixture", result="Before supplement")
                        native_id = calls[-1][1]
                        yield UserMessage("Supplement", uuid=native_id)
                    yield ResultMessage("success", 1, 1, False, 1, "fixture", result="After supplement" if mode != "plain" else "Plain result")

            from qoder_agent_sdk import qodercli_auth
            with patch("qoder_agent_sdk.QoderSDKClient", Client), \
                    patch("agentteams_qoder.runtime.authentication", return_value=qodercli_auth()), \
                    patch("agentteams_qoder.runtime.instructions", return_value="fixture"), \
                    patch("agentteams_qoder.native.NativeTurn.admit"), \
                    patch("worker_matrix_raw.raw_room_history", return_value=[]), \
                    patch("worker_sessions.WorkerSessions.load", return_value={"config_generation": 1}), \
                    patch("worker_sessions.WorkerSessions.usable", return_value=False), \
                    patch("worker_sessions.WorkerSessions.remember"):
                source = {"key": "original", "event_id": "$original", "room_id": "!dm:test", "input": {"sender": "@human:test", "content": {"body": "original"}}}
                if mode.startswith("stop"):
                    with self.assertRaises(ValueError if mode == "stop_unknown" else asyncio.CancelledError):
                        await runtime.execute(source, {"event_key": "original", "generation": 1})
                else:
                    result = await runtime.execute(source, {"event_key": "original", "generation": 1})
                    self.assertEqual(result, "Plain result" if mode == "plain" else "After supplement")
            self.assertIsNone(runtime.active)
            self.assertIsNone(runtime.store.committed)
            if tracing:
                runtime.telemetry.shutdown()
            return runtime, calls

    async def test_steer_commit_consumption_and_following_result_are_required(self):
        runtime, calls = await self.run_turn("steer")
        self.assertEqual(len(calls), 2)
        self.assertEqual(runtime.matrix.native_committed.call_count, 2)
        runtime.matrix.message_finish_tools.assert_called_once()
        self.assertEqual(runtime.matrix.message_cancel.call_args.args[0], {"event_key": "control"})

    async def test_stop_only_reports_stopped_after_native_terminal(self):
        runtime, calls = await self.run_turn("stop")
        self.assertIn("interrupt", calls)
        statuses = [call.args[1]["com.agentteams.execution"]["status"] for call in runtime.client.send_content.call_args_list if "com.agentteams.execution" in call.args[1]]
        self.assertEqual(statuses, ["running", "stopping", "stopped"])
        self.assertEqual(runtime.matrix.message_cancel.call_args.args[0], {"event_key": "original", "generation": 1})

    async def test_unknown_tool_cannot_be_reported_as_stopped(self):
        runtime, _ = await self.run_turn("stop_unknown")
        statuses = [call.args[1]["com.agentteams.execution"]["status"] for call in runtime.client.send_content.call_args_list if "com.agentteams.execution" in call.args[1]]
        self.assertIn("unavailable", statuses)
        self.assertNotIn("stopped", statuses)
        self.assertTrue(all(call.args[0].get("event_key") != "original" for call in runtime.matrix.message_cancel.call_args_list))

    async def test_native_callbacks_inherit_the_turn_trace_for_shared_cli_propagation(self):
        from opentelemetry.trace import get_current_span
        before = get_current_span().get_span_context()
        runtime, _ = await self.run_turn("plain", tracing=True)
        self.assertIn("traceparent", runtime.fixture_carrier)
        self.assertEqual(get_current_span().get_span_context(), before)
