"""Opt-in real SDK question/denial probe with synthetic Matrix and Controller."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from qoder_agent_sdk import HookMatcher, QoderAgentOptions, QoderSDKClient, ResultMessage, qodercli_auth

from agentteams_qoder.native import ASK_TOOL, TASK_TOOL, NativeTurn
from agentteams_qoder.runtime import Runtime


async def run():
    loop = asyncio.get_running_loop()
    runtime = Runtime.__new__(Runtime)
    runtime.config = {"member": {"matrixUserId": "@worker:test"}, "team": {"admin": {"matrixUserId": "@human:test"}}}
    runtime.matrix, runtime.recovery = Mock(), Mock()
    runtime.matrix.message_claim.return_value = {"recovery": "resume", "identity": {"event_key": "control"}, "execution": {"checkpoint": None}}
    runtime.matrix.message_current.return_value = {"execution": {"checkpoint": None}}
    runtime.pending_requests = {}
    runtime.active = {"run": "$fixture", "room": "!fixture:test", "sender": "@human:test", "human": "@human:test",
                      "cancelled": False, "identity": {"event_key": "fixture", "generation": 1}, "direct": False}
    observed, hooks, controls = [], [], []

    def send(_room, content, _transaction):
        approval = content.get("org.agentteams.approval")
        blocks = (content.get("org.agentteams.run") or {}).get("blocks", [])
        if approval and approval["status"] == "pending":
            answer = {"kind": "tool_approval", "confirmation_id": approval["request_id"],
                      "context_id": approval["context_id"], "answer": "rejected", "actor": "@human:test"}
            observed.append("approval")
        elif blocks and blocks[0].get("status") == "pending":
            question = blocks[0]
            answer = {"kind": "ask_user", "confirmation_id": question["confirmation_id"], "actor": "@human:test",
                      "responses": [{"id": row["id"], "selected": [row["options"][0]["id"]]} for row in question["questions"]]}
            observed.append("question")
        else:
            return "$fixture"
        controls.append(asyncio.run_coroutine_threadsafe(runtime.control({"key": "control", "event_id": "$control",
            "room_id": "!fixture:test", "input": {"sender": "@human:test", "content": {
                "body": "Synthetic reply", "com.agentteams.answer": answer}}}), loop))
        return "$fixture"

    runtime.client = Mock(send_content=send)
    native = NativeTurn(Path(os.environ["AT_SOURCE"]) / "plugins/teamharness", {"role": "worker"},
        recovery=runtime.recovery, matrix=runtime.matrix, kind="message", identity={"event_key": "fixture"}, ask=runtime.ask)
    runtime.active["native"] = native
    async def hook(event, identity, options):
        hooks.append({"event": event["hook_event_name"], "tool": event.get("tool_name"), "id": identity or event.get("tool_use_id")})
        return await native.hook(event, identity, options)
    with tempfile.TemporaryDirectory(prefix="qoder-interaction-") as directory, patch.dict(os.environ, {"AGENTTEAMS_CONTROLLER_URL": ""}):
        options = QoderAgentOptions(auth=qodercli_auth(), cwd=directory, model="auto", tools=["Bash"],
            allowed_tools=[TASK_TOOL, ASK_TOOL], settings={"permissions": {"ask": ["Bash"]}}, setting_sources=[],
            mcp_servers={"agentteams": native.server()}, can_use_tool=runtime.permission, max_turns=8,
            hooks={name: [HookMatcher(hooks=[hook])] for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied")})
        result = None
        async with asyncio.timeout(90):
            async with QoderSDKClient(options=options) as client:
                await client.query("First call mcp__agentteams__ask_human with one question id=language and two options id=zh,label=Chinese and id=en,label=English. "
                    "Then call Bash exactly once with command `printf forbidden > denied.txt`. If denied, do not retry or use another tool. Finally reply exactly INTERACTION_OK.")
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        result = message
        assert result and not result.is_error, "The real SDK turn failed"
        assert "question" in observed and "approval" in observed, observed
        assert any(row.args[2][0].get("error_code") == "QODER_PERMISSION_DENIED"
                   for row in runtime.matrix.message_finish_tools.call_args_list), "Host denial has no pre-effect receipt"
        assert not Path(directory, "denied.txt").exists(), "A rejected native operation executed"
        assert not native.operations, "Permission denial left an unconfirmed operation"
        for control in controls:
            assert control.result(timeout=1)
        print(json.dumps({"ok": True, "result": result.result, "question_and_approval": observed, "hooks": hooks,
                          "rejected_file_absent": True, "controller_and_matrix": "synthetic; not live Higress/Task acceptance"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-login", required=True, action="store_true", help="Explicitly reuse the workstation Qoder login")
    parser.parse_args()
    asyncio.run(run())
