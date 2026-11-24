"""Check emitted Qoder payloads against the actual pinned Dashboard parsers."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from qoder_agent_sdk import AssistantMessage, ThinkingBlock

from agentteams_qoder.display import TurnDisplay
from test_interaction import runtime, source


async def fixtures():
    instance = runtime()
    request = asyncio.create_task(instance.permission("Bash", {"command": "echo fixture"}, SimpleNamespace(tool_use_id="fixture", signal=None)))
    async with asyncio.timeout(2):
        while not instance.client.send_content.called:
            await asyncio.sleep(0)
    approval = instance.client.send_content.call_args.args[1]["org.agentteams.approval"]
    await instance.control(source("批准", **{"com.agentteams.answer": {"kind": "tool_approval", "confirmation_id": approval["request_id"],
        "context_id": approval["context_id"], "answer": "approved", "actor": "@human:test"}}))
    await request
    question = asyncio.create_task(instance.ask({"questions": [{"id": "color", "question": "颜色", "options": [{"id": "blue", "label": "蓝色"}]}]}))
    async with asyncio.timeout(2):
        while not any("org.agentteams.run" in call.args[1] for call in instance.client.send_content.call_args_list):
            await asyncio.sleep(0)
    pending = [call.args[1]["org.agentteams.run"] for call in instance.client.send_content.call_args_list if "org.agentteams.run" in call.args[1]][-1]
    await instance.control(source("蓝色", **{"com.agentteams.answer": {"kind": "ask_user", "confirmation_id": pending["blocks"][0]["confirmation_id"],
        "responses": [{"id": "color", "selected": ["blue"]}], "actor": "@human:test"}}))
    await question
    display = TurnDisplay(instance.client, {"room_id": "!room", "event_id": "$fixture"}, direct=True)
    await display.tool("Read", "native-call", {"file_path": "fixture"})
    await display.tool("Read", "native-call", result="done")
    await display.message(AssistantMessage([ThinkingBlock("synthetic", "")], "fixture", message_id="native"))
    await display.status("running")
    runs = [call.args[1].get("m.new_content", call.args[1])["org.agentteams.run"]
            for call in instance.client.send_content.call_args_list if "org.agentteams.run" in call.args[1]]
    return {"approval": approval, "runs": runs, "status": instance.client.send_content.call_args.args[1]}


CHECK = """
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const ts=require(process.argv[2] || 'typescript'),root=process.argv[1];
function load(file){const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(root,file),'utf8'),
  {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports});return exports;}
const data=JSON.parse(fs.readFileSync(0,'utf8')),a=load('src/lib/runtime-approval.ts'),
  p=load('src/lib/a2ui/protocol.ts'),e=load('src/lib/direct-execution.ts');
assert(a.parseRuntimeApproval(data.approval));assert(a.runtimeApprovalPayload(data.approval).structuredReply);
let thinking=0,tools=0,questions=0;
for(const run of data.runs)for(const block of run.blocks){
  if(block.type==='tool_call'){assert(p.normalizeToolCallPayload(block));tools++;}
  if(block.type==='confirmation'){assert(p.normalizeConfirmationPayload(block));questions++;}
  if(block.type==='thinking'){assert.strictEqual(typeof block.content,'string');thinking++;}}
assert(thinking && tools===2 && questions===2);
assert(e.getDirectExecution([{type:'m.room.message',sender:'@worker:test',origin_server_ts:1,content:data.status}],
  '@human:test',['@human:test','@worker:test'],['@worker:test']));
console.log(JSON.stringify({ok:true,approval:true,question_receipt:true,tool_blocks:true,thinking:true,direct_controls:true}));
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--typescript", default="typescript", help="TypeScript module path, or resolve from Dashboard dependencies")
    args = parser.parse_args()
    subprocess.run([args.node, "-e", CHECK, str(args.dashboard.resolve()), args.typescript],
        input=json.dumps(asyncio.run(fixtures())), text=True, cwd=args.dashboard, check=True)
