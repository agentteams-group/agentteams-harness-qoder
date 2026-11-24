"""Exercise the full SDK options/turn adapter with stubbed Controller authority."""
import asyncio
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from agentteams_qoder.core import common
from agentteams_qoder.runtime import Runtime
from agentteams_qoder.sessions import SessionStore
from test_config import record


async def main():
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
        runtime.matrix.native_input.return_value = {}
        runtime.store = SessionStore(root / "runtime/sessions.sqlite")
        runtime.telemetry, runtime.active, runtime.pending_approvals = None, None, {}
        source = {"key": "source-key", "event_id": "$native-turn", "room_id": "!dm:test",
                  "input": {"sender": "@human:test", "content": {"msgtype": "m.text", "body":
                    "Read probe.txt with Read, then call mcp__agentteams__task with action=get and taskId=sdk-probe. Finally reply exactly NATIVE_RUNTIME_OK."}}}
        identity = {"event_key": "source-key", "instance_id": "i", "process_id": "p", "generation": 1}
        view = {"config_generation": 1, "cache": None}
        from worker_sessions import WorkerSessions
        cli = common(runtime.plugin, "task_cli")
        with patch.dict(os.environ, {"QODER_AUTH_MODE": "local", "AGENTTEAMS_CONTROLLER_URL": ""}), \
                patch("agentteams_qoder.runtime.gateway_options", return_value={"model": "auto", "settings": {}, "env": {}}), \
                patch.object(WorkerSessions, "load", return_value=view), \
                patch.object(WorkerSessions, "remember") as remember, \
                patch.object(WorkerSessions, "usable", return_value=False), \
                patch("worker_matrix_raw.raw_room_history", return_value=[]), \
                patch.object(cli, "run", return_value={"ok": True, "task": {"id": "sdk-probe", "status": "assigned"}}):
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
                    answer = await runtime.execute(source, identity)
        assert answer == "NATIVE_RUNTIME_OK", answer
        runtime.matrix.native_committed.assert_called_once()
        remember.assert_called_once()
        print({"ok": True, "result": answer, "adapter": "Runtime.execute", "controller_and_model_routing": "stubbed; not Higress or end-to-end acceptance"})


if __name__ == "__main__":
    asyncio.run(main())
