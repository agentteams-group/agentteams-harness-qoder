"""Real native SDK stop boundary; Core effects are stubbed, not E2E certification."""
import asyncio,json,os,tempfile,uuid
from pathlib import Path
from unittest.mock import Mock,patch
from qoder_agent_sdk import QoderAgentOptions,QoderSDKClient,HookMatcher,ResultMessage,AssistantMessage,qodercli_auth
from agentteams_qoder.native import NativeTurn,TASK_TOOL
from agentteams_qoder.core import common
from agentteams_qoder.sessions import SessionStore

async def run():
    plugin=Path(os.environ['AT_SOURCE'])/'plugins/teamharness'
    recovery=Mock();recovery.tools_current.return_value={'execution':{'checkpoint':None}}
    turn=NativeTurn(plugin,{'role':'worker','sourceEvent':'$fixture','execution':'sdk-probe'},
        recovery=recovery,matrix=Mock(),kind='task',identity={'task_id':'sdk-probe'})
    terminal={'task':{'id':'sdk-probe','mainTaskId':'main','status':'completed','submissionId':'s1',
        'handoff':{'submissionId':'s1','actorRole':'worker','actorName':'probe'}},
        'mainProjection':'applied','dispatchStatus':'ready','assignments':[]}
    with tempfile.TemporaryDirectory(prefix='qoder-phase-stop-') as directory:
        store=SessionStore(Path(directory)/'sessions.sqlite');session=str(uuid.uuid4());after=[]
        async def resolve_model(context):
            if turn.complete:after.append(context)
            return {'model':'auto'}
        options=QoderAgentOptions(auth=qodercli_auth(),cwd=directory,tools=[],setting_sources=[],model='auto',
            mcp_servers={'agentteams':turn.server()},allowed_tools=[TASK_TOOL],
            hooks={name:[HookMatcher(hooks=[turn.hook])] for name in ['PreToolUse','PostToolUse','PostToolUseFailure']},
            session_store=store,session_store_flush='eager',session_id=session,max_turns=3,resolve_model=resolve_model)
        with patch.object(common(plugin,'task_cli'),'run',return_value=terminal):
            async with asyncio.timeout(90):
                async with QoderSDKClient(options=options) as client:
                    await client.query('Call '+TASK_TOOL+' exactly once with action=submit,taskId=sdk-probe,summary=Done,status=SUCCESS. Do not call other tools.')
                    async for message in client.receive_response():
                        if isinstance(message,ResultMessage):break
        with store.connect() as db:
            stored=[{'session':row[0],'value':json.loads(row[1])} for row in db.execute('SELECT session,value FROM entries ORDER BY sequence')]
        evidence=Path(os.environ.get('AT_PHASE_EVIDENCE','/tmp/qoder-phase-synthetic-evidence.json'))
        evidence.write_text(json.dumps({'requestedSession':session,'callId':turn.terminal_call_id,'receiptId':turn.completion.get('receiptId') if turn.completion else None,'rows':stored},ensure_ascii=False))
        evidence.chmod(0o600)
        assert turn.complete and turn.terminal_call_id,'No native terminal Task hook'
        assert store.has_task_receipt(session,turn.terminal_call_id,turn.completion['receiptId']),'Missing actual SDK tool-result row'
        assert not after,'Another model resolution followed the terminal Task tool'
        tool_row=next(i for i,row in enumerate(stored) if turn.terminal_call_id in json.dumps(row['value']) and row['value'].get('type')=='user')
        assert not any(row['value'].get('type')=='assistant' for row in stored[tool_row+1:]),'Assistant history followed the terminal tool result'
        print(json.dumps({'ok':True,'terminal_receipt_persisted':True,'assistant_messages_after_submit':len(after),
            'core':'stubbed; native SDK boundary only','model':'Qoder account auto'}))

if __name__=='__main__':asyncio.run(run())
