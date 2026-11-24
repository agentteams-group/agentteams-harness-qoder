"""Exercise the full SDK options/turn adapter with stubbed Controller authority."""
import asyncio
import argparse
import os
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from agentteams_qoder.core import common
from agentteams_qoder.runtime import Runtime
from agentteams_qoder.sessions import SessionStore
from test_config import record


async def main(steer=False, stop=False):
    with tempfile.TemporaryDirectory(prefix="qoder-turn-acceptance-") as directory:
        root = Path(directory)
        (root / "workspace").mkdir()
        (root / "workspace/probe.txt").write_text("NATIVE_RUNTIME_OK\n")
        runtime = Runtime.__new__(Runtime)
        runtime.home, runtime.plugin = root, Path(os.environ["AT_SOURCE"]) / "plugins/teamharness"
        runtime.config, runtime.role = record(), "worker"
        runtime.model = {"model": "unused", "api_key": "fixture", "url": "http://unused/v1"}
        runtime.recovery, runtime.matrix = Mock(), Mock()
        runtime.client = Mock()
        runtime.matrix.message_current.return_value = {"execution": {"checkpoint": None}}
        runtime.matrix.message_begin_tools.return_value = "op"
        runtime.matrix.message_claim.return_value = {"recovery": "resume", "execution": {"checkpoint": None},
                                                    "identity": {"event_key": "control", "generation": 1}}
        runtime.matrix.native_input.return_value = {}
        runtime.store = SessionStore(root / "runtime/sessions.sqlite")
        transcript_rows = []
        append = runtime.store.append
        async def record_rows(key, rows):
            transcript_rows.extend({"type": row.get("type"), "uuid": row.get("uuid"), "keys": sorted(row)} for row in rows)
            await append(key, rows)
        runtime.store.append = record_rows
        runtime.telemetry, runtime.active, runtime.pending_requests = None, None, {}
        source = {"key": "source-key", "event_id": "$native-turn", "room_id": "!dm:test",
                  "input": {"sender": "@human:test", "content": {"msgtype": "m.text", "body":
                    "Read probe.txt with Read, then call mcp__agentteams__task with action=get and taskId=sdk-probe. Finally reply exactly NATIVE_RUNTIME_OK."}}}
        controls = []
        if stop:
            source["input"]["content"]["body"] = "Explain durable task systems. Do not call any tools."
        if steer:
            source["input"]["content"]["body"] = (
                "Call mcp__agentteams__ask_human with one question id=ready and two options id=yes,label=Ready and id=no,label=Wait. "
                "After the answer, reply exactly BASE_RESULT unless a later user message changes the output.")
            loop = asyncio.get_running_loop()
            async def respond(block):
                supplement = {"key": "steer-key", "event_id": "$steer-live", "room_id": "!dm:test", "input": {
                    "sender": "@human:test", "content": {"body": "After answering the question, reply exactly STEER_LIVE_OK.",
                    "com.agentteams.control": {"action": "steer", "agent_id": "@worker:test", "run_id": "$native-turn"}}}}
                assert await runtime.control(supplement)
                answer = {"key": "answer-key", "event_id": "$answer-live", "room_id": "!dm:test", "input": {
                    "sender": "@human:test", "content": {"body": "Ready", "com.agentteams.answer": {
                        "kind": "ask_user", "actor": "@human:test", "confirmation_id": block["confirmation_id"],
                        "responses": [{"id": "ready", "selected": ["yes"]}]}}}}
                assert await runtime.control(answer)
            def send(_room, content, _transaction):
                blocks = (content.get("org.agentteams.run") or {}).get("blocks", [])
                if blocks and blocks[0].get("status") == "pending" and blocks[0].get("questions"):
                    controls.append(asyncio.run_coroutine_threadsafe(respond(blocks[0]), loop))
                return "$fixture-display"
            runtime.client.send_content.side_effect = send
        identity = {"event_key": "source-key", "instance_id": "i", "process_id": "p", "generation": 1}
        view = {"config_generation": 1, "cache": None}
        from worker_sessions import WorkerSessions
        cli = common(runtime.plugin, "task_cli")
        from agentteams_qoder.telemetry import NativeTrace
        model_start = NativeTrace.model_start
        def on_model_start(trace, context):
            model_start(trace, context)
            if stop and not controls:
                controls.append(asyncio.create_task(runtime.control({"key": "stop-key", "event_id": "$stop-live", "room_id": "!dm:test", "input": {
                    "sender": "@human:test", "content": {"body": "停止当前执行", "com.agentteams.control": {
                        "action": "stop", "agent_id": "@worker:test", "run_id": "$native-turn"}}}})))
        with patch.dict(os.environ, {"QODER_AUTH_MODE": "local", "AGENTTEAMS_CONTROLLER_URL": ""}), \
                patch("agentteams_qoder.runtime.gateway_options", return_value={"model": "auto", "settings": {}, "env": {}}), \
                patch.object(WorkerSessions, "load", return_value=view), \
                patch.object(WorkerSessions, "remember") as remember, \
                patch.object(WorkerSessions, "usable", return_value=False), \
                patch("worker_matrix_raw.raw_room_history", return_value=[]), \
                patch.object(cli, "run", return_value={"ok": True, "task": {"id": "sdk-probe", "status": "assigned"}}), \
                patch.object(NativeTrace, "model_start", on_model_start):
            # The opt-in local smoke uses the existing local account. Production
            # uses the private mounted member profile, never this workstation path.
            import qoder_agent_sdk
            original = qoder_agent_sdk.QoderAgentOptions
            def options(**kwargs):
                kwargs.pop("env")
                return original(**kwargs)
            with patch.object(qoder_agent_sdk, "QoderAgentOptions", side_effect=options), \
                    patch.object(qoder_agent_sdk.QoderSDKClient, "get_available_models",
                                 return_value=[{"value": "auto", "isEnabled": True}]):
                async with asyncio.timeout(90):
                    try:
                        answer = await runtime.execute(source, identity)
                    except asyncio.CancelledError:
                        if not stop:
                            raise
                        answer = "NATIVE_STOP_OK"
                    except Exception:
                        if steer:
                            print(json.dumps({"synthetic_transcript_rows": transcript_rows,
                                "committed_source_keys": [call.args[0] for call in runtime.matrix.native_committed.call_args_list]}))
                        raise
        assert answer == ("NATIVE_STOP_OK" if stop else "STEER_LIVE_OK" if steer else "NATIVE_RUNTIME_OK"), answer
        assert runtime.matrix.native_committed.call_count == (2 if steer else 1)
        for control in controls:
            if stop:
                assert await control
            else:
                control.result(timeout=1)
        if stop:
            statuses = [call.args[1]["com.agentteams.execution"]["status"] for call in runtime.client.send_content.call_args_list if "com.agentteams.execution" in call.args[1]]
            assert statuses == ["running", "stopping", "stopped"], statuses
            remember.assert_not_called()
        else:
            remember.assert_called_once()
        print({"ok": True, "result": answer, "adapter": "Runtime.execute", "steer": steer, "stop": stop,
               "native_input_commits": runtime.matrix.native_committed.call_count,
               "controller_and_model_routing": "stubbed; not Higress or end-to-end acceptance"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--steer", action="store_true", help="Exercise a real SDK safe-boundary supplement")
    mode.add_argument("--stop", action="store_true", help="Interrupt a real SDK inference turn")
    args = parser.parse_args()
    asyncio.run(main(args.steer, args.stop))
