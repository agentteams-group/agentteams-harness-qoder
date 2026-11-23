"""Opt-in real inference; public Task API effects are stubbed, never certified."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import uuid

from qoder_agent_sdk import HookMatcher, QoderAgentOptions, QoderSDKClient, ResultMessage, qodercli_auth

from agentteams_qoder.config import authentication
from agentteams_qoder.core import common
from agentteams_qoder.native import NativeTurn, TASK_TOOL
from agentteams_qoder.sessions import SessionStore


async def run(local_login):
    plugin = Path(os.environ["AT_SOURCE"]) / "plugins/teamharness"
    recovery = Mock()
    recovery.tools_current.return_value = {"execution": {"checkpoint": None}}
    recovery.begin_tools.return_value = "sdk-probe-operation"
    native = NativeTurn(plugin, {"role": "worker", "sourceEvent": "$sdk-probe"},
                        recovery=recovery, matrix=Mock(), kind="task", identity={"task_id": "sdk-probe"})
    hooks = []
    async def hook(event, call_id, options):
        hooks.append({"event": event["hook_event_name"], "tool": event.get("tool_name"), "id": call_id or event.get("tool_use_id")})
        return await native.hook(event, call_id, options)
    with tempfile.TemporaryDirectory(prefix="qoder-sdk-acceptance-") as directory:
        root = Path(directory)
        (root / "probe.txt").write_text("NATIVE_READ_OK\n")
        store = SessionStore(root / "sessions.sqlite")
        prompt_id = str(uuid.uuid4())
        committed = []
        async def on_commit(key, entries):
            committed.extend(row for row in entries if row.get("uuid") == prompt_id)
        store.committed = on_commit
        options = QoderAgentOptions(auth=qodercli_auth() if local_login else authentication(), cwd=root,
            system_prompt="You are testing native tool integration. Follow the user request exactly.",
            tools=["Read"], allowed_tools=["Read", TASK_TOOL], setting_sources=[],
            mcp_servers={"agentteams": native.server()}, model="auto", max_turns=5,
            hooks={name: [HookMatcher(hooks=[hook])] for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure")},
            session_store=store, session_store_flush="eager")
        result = None
        cli = common(plugin, "task_cli")
        with patch.object(cli, "run", return_value={"ok": True, "task": {"id": "sdk-probe", "status": "assigned"}}) as task_api:
            async with asyncio.timeout(90):
                async with QoderSDKClient(options=options) as client:
                    status = await client.get_mcp_status()
                    print(json.dumps({"mcp": [{"name": server.get("name"), "status": server.get("status"),
                        "tools": [tool.get("name") for tool in server.get("tools", [])]} for server in status["mcpServers"]]}))
                    await client.query("First read probe.txt with Read. Then call " + TASK_TOOL +
                                       " with action=get and taskId=sdk-probe. Finally reply exactly SDK_TOOLS_OK.", message_uuid=prompt_id)
                    async for message in client.receive_response():
                        if isinstance(message, ResultMessage):
                            result = message
        assert result and not result.is_error, "Real native turn failed"
        assert task_api.called, "The real SDK did not invoke the public Task binding"
        assert recovery.begin_tools.called and recovery.finish_tools.called, "Native file tool bypassed the shared journal"
        assert committed, "The SDK did not mirror the exact native user input"
        assert hooks and all(row["id"] for row in hooks), "Native hook call identity is absent"
        assert not native.operations, "Native tool operation is still unconfirmed"
        print(json.dumps({"ok": True, "result": result.result, "native_hooks": hooks,
                          "input_committed": True, "task_api": "stubbed; not protocol acceptance"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-login", action="store_true", help="Explicitly reuse this workstation's Qoder login")
    asyncio.run(run(parser.parse_args().local_login))
