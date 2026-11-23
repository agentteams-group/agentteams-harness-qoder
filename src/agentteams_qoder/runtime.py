"""Qoder SDK execution with Controller-owned input, Task and reply recovery."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote
import uuid

from .config import authentication, external_mcp, instructions, load, model_selection
from .core import common
from .native import NativeTurn, TASK_TOOL
from .sessions import SessionStore
from .transport import MatrixClient, allowed
from .telemetry import NativeTrace, configure


class Runtime:
    def __init__(self):
        from task_recovery import Recovery
        from worker_matrix_raw import RawWorkerSync
        from worker_matrix_state import WorkerMatrix

        self.path = Path(os.environ["TEAMHARNESS_RUNTIME_CONFIG"])
        self.home = self.path.parent.parent
        self.plugin = Path(os.environ["AGENTTEAMS_PLUGIN_DIR"])
        self.recovery = Recovery(device_delivery=True)
        self.matrix = WorkerMatrix("qoder", recovery=self.recovery)
        self.config, self.role, matrix, self.model = load(self.path)
        self.client = MatrixClient(matrix)
        self.inbox = RawWorkerSync(self.matrix, self.client, self.config["member"]["matrixUserId"],
                                    self.classify, self.observe_rooms)
        self.joined = set()
        self.active = None
        self.pending_approvals = {}
        self.last_sync = 0
        self.telemetry = configure()
        prefix = os.environ["AGENTTEAMS_STORAGE_PREFIX"].rstrip("/")
        member = self.config["member"]["runtimeName"]
        self.store = SessionStore(self.home / "runtime/qoder-sessions.sqlite",
                                  prefix + f"/agents/{member}/runtime/qoder-sessions.sqlite")

    def classify(self, room, event):
        from task_coordination import IgnoredNotification, notification

        try:
            packet = notification(event.get("content") or {}, room, event.get("sender"),
                                  self.config["member"]["matrixUserId"], event_id=event["event_id"])
        except IgnoredNotification:
            return None
        if packet:
            coordination = packet["meta"]["teamharness_coordination"]
            return ("task_notification", coordination["task_id"]) if coordination["kind"] == "task_assigned" else ("message", None)
        return ("message", None) if allowed(self.config, room, event) else None

    def observe_rooms(self, payload):
        from worker_matrix_raw import require_unencrypted_room

        for room, info in payload.get("rooms", {}).get("invite", {}).items():
            invites = info.get("invite_state", {}).get("events", [])
            if any(event.get("type") == "m.room.member" and event.get("state_key") == self.config["member"]["matrixUserId"]
                   and allowed(self.config, room, {**event, "content": {"msgtype": "m.text", "body": "invite"}}, invitation=True)
                   for event in invites):
                require_unencrypted_room(self.client, room)
                self.client.request("POST", "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/join", {})
        self.joined.update(payload.get("rooms", {}).get("join", {}))
        self.joined.difference_update(payload.get("rooms", {}).get("leave", {}))
        return self.joined

    async def permission(self, name, arguments, context):
        from qoder_agent_sdk import PermissionResultAllow, PermissionResultDeny

        active = self.active
        if not active or not context.tool_use_id:
            return PermissionResultDeny(message="No active native tool identity")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = (active["run"], future)
        metadata = {"version": 1, "runtime": "qoder", "request_id": request_id,
                    "context_id": active["run"], "status": "pending", "tool_name": name,
                    "parameters": json.dumps(arguments, ensure_ascii=False),
                    "expires_at": int((time.time() + 300) * 1000)}
        try:
            await asyncio.to_thread(self.client.send, active["room"],
                {"msgtype": "m.text", "body": f"Qoder requests permission for {name}. Use /approve {request_id} {active['run']} or /reject {request_id} {active['run']}.",
                 "org.agentteams.approval": metadata}, "qoder-approval:" + request_id)
            approved = await asyncio.wait_for(asyncio.shield(future), timeout=300)
            return PermissionResultAllow(updated_input=arguments) if approved else PermissionResultDeny(message="Human rejected this operation")
        except TimeoutError:
            return PermissionResultDeny(message="Approval expired")
        finally:
            self.pending_approvals.pop(request_id, None)
            status = ("approved" if future.result() else "rejected") if future.done() and not future.cancelled() else "expired"
            await asyncio.to_thread(self.client.send, active["room"],
                {"msgtype": "m.text", "body": "Qoder permission request closed.",
                 "org.agentteams.approval": {**metadata, "status": status}},
                "qoder-approval-closed:" + request_id)

    async def control(self, item):
        from task_operation import admit

        text = item["input"]["content"]["body"]
        verb, _, argument = text.partition(" ")
        wire = item["input"]["content"].get("org.agentteams.execution_control")
        if wire:
            if not isinstance(wire, dict) or not self.active or wire.get("agent_id") != self.config["member"]["matrixUserId"] or wire.get("run_id") != self.active["run"]:
                return False
            verb = {"stop": "/cancel", "steer": "/steer"}.get(wire.get("action"), "")
            argument = text
        if verb not in {"/cancel", "/steer", "/approve", "/reject"}:
            return False
        active = self.active
        sender = item["input"]["sender"]
        admin = self.config.get("team", {}).get("admin", {}).get("matrixUserId")
        authorized = active and item["room_id"] == active["room"] and sender in {active["sender"], admin}
        # Agent-originated turns may only be controlled by the Human admin.
        if authorized and sender != admin:
            authorized = sender == self.config["member"].get("adminMatrixUserId") or sender == active.get("human")
        if not authorized:
            return False
        key = item["key"]
        claim = await asyncio.to_thread(self.matrix.message_claim, key)
        if claim["recovery"] != "resume":
            return True
        admit(claim["execution"].get("checkpoint"))
        identity = claim["identity"]
        call = {"id": item["event_id"], "name": "qoder.control", "arguments": {"verb": verb, "run": active["run"]}}
        operation = await asyncio.to_thread(self.matrix.message_begin_tools, identity, [call])
        if verb in {"/approve", "/reject"}:
            request_id, _, run = argument.strip().partition(" ")
            pending = self.pending_approvals.get(request_id)
            if pending and pending[0] == active["run"] and (not run or run == active["run"]):
                if not pending[1].done():
                    pending[1].set_result(verb == "/approve")
        elif verb == "/cancel":
            if active["kind"] != "message":
                raise ValueError("Cancel the assigned Task through its public main Task")
            await asyncio.to_thread(self.matrix.message_cancel, active["identity"])
            await active["client"].interrupt()
            active["cancelled"] = True
        elif argument.strip():
            await active["client"].query(argument.strip(), priority="now", message_uuid=str(uuid.uuid4()))
        await asyncio.to_thread(self.matrix.message_finish_tools, identity, operation,
                                [{"id": item["event_id"], "content": "native control acknowledged"}])
        await asyncio.to_thread(self.matrix.message_cancel, identity)
        return True

    def prepare(self, source, *, recovered=None):
        from task_coordination import notification

        context = {"role": self.role, "sourceEvent": source["event_id"], "sources": []}
        text = source["input"]["content"]["body"]
        packet = None
        if not recovered:
            packet = notification(source["input"]["content"], source["room_id"], source["input"]["sender"],
                                  self.config["member"]["matrixUserId"], event_id=source["event_id"])
        if packet:
            event = dict(packet["meta"]["teamharness_coordination"])
            admission = common(self.plugin, "task_native").invoke({"operation": "admit_event",
                "eventId": event["id"], "nativeEventId": source["event_id"]})
            if admission.get("active") is not True:
                raise ValueError("Coordination phase is no longer active")
            if event["kind"] == "handoff":
                event["pending_task_ids"] = admission.get("pending_task_ids")
            text = packet["text"]
        elif recovered:
            task = recovered["task"]
            event = {"kind": "task_assigned", "task_id": task["task_id"],
                     "main_task_id": task.get("main_task_id", ""), "project_id": task.get("project_id", ""),
                     "delivery_id": task.get("notifyTxn")}
        else:
            event = None
        if event:
            prepared = common(self.plugin, "task_native").invoke({"operation": "prepare_event", "event": event})
            if prepared.get("ok") is False:
                raise ValueError("Task preparation is not available")
            context.update(prepared)
            text += "\n\n" + prepared.get("fact", "") + "\n" + json.dumps(prepared.get("context"), ensure_ascii=False)
        return context, text

    async def execute(self, source, identity, *, recovered=None):
        from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, HookMatcher, ResultMessage, AssistantMessage
        from worker_sessions import WorkerSessions

        context, text = await asyncio.to_thread(self.prepare, source, recovered=recovered)
        kind = "task" if recovered else "message"
        native = NativeTurn(self.plugin, context, recovery=self.recovery, matrix=self.matrix, kind=kind, identity=identity)
        await asyncio.to_thread(native.admit)
        work = {"kind": "task", "team_id": identity["team_id"], "task_id": identity["task_id"]} if recovered else {"kind": "message", "event_key": identity["event_key"]}
        sessions = WorkerSessions("qoder", recovery=self.recovery)
        view = await asyncio.to_thread(sessions.load, work)
        config_generation = view["config_generation"]
        cache = view.get("cache") or {}
        resume = cache.get("native_ref")
        if not sessions.usable(view, resume, self.store.digest(resume)):
            resume = None
        if not resume and not recovered and source["event_id"].startswith("$"):
            from worker_matrix_raw import raw_room_history
            history = await asyncio.to_thread(raw_room_history, self.client, self.matrix,
                self.config["member"]["matrixUserId"], source["room_id"], source["event_id"],
                lambda room, raw: allowed(self.config, room, raw))
            if history:
                text = "Earlier confirmed conversation:\n" + json.dumps(history, ensure_ascii=False) + "\n\nCurrent input:\n" + text
        session_id = resume or str(uuid.uuid4())
        trace = NativeTrace(self.telemetry, role=self.role, source=source, identity=identity, session=session_id, context=context)
        native.trace = trace
        prompt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source["event_id"]))
        persisted = asyncio.Event()

        async def committed(key, rows):
            if key.get("subpath"):
                raise ValueError("Nested transcripts are not supported by this candidate")
            for row in rows:
                if row.get("type") == "user" and row.get("uuid") == prompt_id:
                    if source.get("key"):
                        snapshot = await asyncio.to_thread(self.matrix.native_input, source["key"])
                        digest = hashlib.sha256(self.store.encode(row).encode()).hexdigest()
                        await asyncio.to_thread(self.matrix.native_committed, source["key"], snapshot,
                                                session_id + ":" + prompt_id, digest)
                    persisted.set()

        self.store.committed = committed
        async def resolve_model(_context):
            trace.model_start(_context)
            return {"model": model_selection(self.model)}

        tools = [] if any(context.get(key) for key in ("planning", "review", "completion")) else ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        options = QoderAgentOptions(cwd=(context.get("context") or {}).get("outputDirectory") or str(self.home / "workspace"),
            system_prompt=instructions(self.config, self.role, self.plugin, self.home) + "\nYour public Task tool is " + TASK_TOOL,
            tools=tools, mcp_servers={"agentteams": native.server(), **(external_mcp(self.config, self.model["api_key"]) if tools else {})},
            allowed_tools=[TASK_TOOL], disallowed_tools=["Task", "Agent", "TaskOutput", "TaskStop"],
            strict_mcp_config=True, setting_sources=[], permission_mode="default", auth=authentication(),
            can_use_tool=self.permission, hooks={
                "PreToolUse": [HookMatcher(hooks=[native.hook])],
                "PostToolUse": [HookMatcher(hooks=[native.hook])],
                "PostToolUseFailure": [HookMatcher(hooks=[native.hook])],
                "Stop": [HookMatcher(hooks=[native.stop])]},
            resolve_model=resolve_model, session_store=self.store, session_store_flush="eager",
            env={"QODER_CONFIG_DIR": str(self.home / ".qoder")},
            resume=resume, session_id=None if resume else session_id, max_turns=50)
        answer = None
        try:
            async with QoderSDKClient(options=options) as client:
                peers = {member.get("matrixUserId") for member in self.config.get("team", {}).get("members", [])}
                requester = source["input"]["sender"]
                human = requester if source["room_id"] == self.config["member"].get("personalRoomId") and requester not in peers else None
                if self.config.get("team") and requester != self.config["team"].get("admin", {}).get("matrixUserId"):
                    human = None  # Team Agent senders cannot approve native side effects.
                self.active = {"run": source["event_id"], "room": source["room_id"], "sender": source["input"]["sender"],
                               "human": human, "client": client, "cancelled": False, "kind": kind, "identity": identity}
                status = await client.get_mcp_status()
                registered = [tool for server in status.get("mcpServers", []) if server.get("name") == "agentteams"
                              for tool in server.get("tools", [])]
                if not any(tool.get("name") in {"task", TASK_TOOL} for tool in registered):
                    raise ValueError("Qoder did not discover the registered public Task tool")
                await client.query(text, message_uuid=prompt_id)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        trace.model_response(message)
                    if isinstance(message, ResultMessage):
                        if message.is_error:
                            raise RuntimeError("Qoder turn failed: " + "; ".join(message.errors or [str(message.subtype)]))
                        answer = message.result or ""
                if self.active["cancelled"]:
                    raise asyncio.CancelledError()
                view = await asyncio.to_thread(sessions.load, work)
            if not persisted.is_set():
                raise ValueError("Qoder did not persist the exact native user input")
            if native.operations:
                raise ValueError("Qoder ended with an unverified native tool operation")
            if any(context.get(key) for key in ("execution", "planning", "review", "completion")) and not native.complete:
                raise ValueError("Qoder ended without a confirmed public Task receipt")
            # Retain native cache only for the snapshot actually represented at
            # input admission. Changed Task checkpoints cause a fresh session.
            if view["config_generation"] == config_generation:
                await asyncio.to_thread(sessions.remember, work, view, identity, session_id,
                                        self.store.digest(session_id), int(time.time()) + 86400)
            return answer
        finally:
            trace.close()
            self.store.committed = None
            self.active = None
            for _, future in self.pending_approvals.values():
                if not future.done():
                    future.set_result(False)

    async def deliver(self, room, identity, execution, text, *, task=False):
        from worker_matrix_delivery import confirm_bound
        from worker_matrix_raw import publish_raw_saved

        def bind(checkpoint):
            if task:
                saved = self.recovery.checkpoint(identity["task_id"], identity["team_id"], checkpoint, expected_identity=identity)
            else:
                saved = self.matrix.message_checkpoint(identity, checkpoint)
            confirm_bound(saved["execution"], identity, "qoder", checkpoint)
        event = await asyncio.to_thread(publish_raw_saved, self.client, room, text, execution["checkpoint"],
                                        self.config["member"]["matrixUserId"], bind,
                                        content={"org.agentteams.progress_only": True} if task else None)
        if task:
            await asyncio.to_thread(self.recovery.delivered, identity["task_id"], identity["team_id"], event)
        else:
            await asyncio.to_thread(self.matrix.message_delivered, identity, event)

    async def process_message(self, source):
        claimed = await asyncio.to_thread(self.matrix.message_claim, source["key"])
        if claimed["recovery"] == "deliver":
            await self.deliver(source["room_id"], claimed["identity"], claimed["execution"], claimed["saved_result"])
        elif claimed["recovery"] == "resume":
            if claimed.get("resume_text") and claimed["execution"].get("checkpoint"):
                source = {**source, "input": {**source["input"], "content": {
                    **source["input"]["content"], "body": claimed["resume_text"]}}}
            answer = await self.execute(source, claimed["identity"])
            saved = await asyncio.to_thread(self.matrix.message_save_reply, claimed["identity"], answer)
            await self.deliver(source["room_id"], claimed["identity"], saved["execution"], answer)

    async def process_task(self, snapshot, entry, inputs):
        claimed = await asyncio.to_thread(self.recovery.claim, snapshot, entry)
        task = claimed["task"]
        execution = task["execution"]
        if execution["state"] == "needs_verification":
            return
        identity = {"task_id": task["task_id"], "team_id": entry["team_id"],
                    **{key: execution[key] for key in ("instance_id", "process_id", "generation")}}
        room = task["room_id"]
        if (execution.get("checkpoint") or {}).get("delivery") == "pending":
            await self.deliver(room, identity, execution, claimed["saved_result"], task=True)
            return
        if task["status"] == "submitted":
            return
        source = next((item for item in inputs if item.get("work") == {"kind": "task", "task_id": identity["task_id"], "team_id": identity["team_id"]}), None)
        resume = self.recovery.resume_text({**task, "team_id": entry["team_id"], "spec": claimed["spec"]})
        if source is None:
            source = {"event_id": "recovery:" + task["task_id"] + ":" + str(execution["generation"]),
                      "room_id": room, "input": {"sender": self.config["member"]["matrixUserId"],
                          "content": {"msgtype": "m.text", "body": resume}}}
        else:
            source = {**source, "input": {**source["input"], "content": {**source["input"]["content"], "body": resume}}}
        answer = await self.execute(source, identity, recovered=claimed)
        saved, answer = await asyncio.to_thread(self.recovery.prepare_reply, identity["task_id"], identity["team_id"], answer)
        await self.deliver(room, identity, saved["execution"], answer, task=True)

    async def run(self):
        from runtime_observability import event
        from worker_handoff import recovery_scanned

        await asyncio.to_thread(self.matrix.start)
        running = None
        while True:
            try:
                snapshot, gaps = await asyncio.to_thread(self.inbox.poll)
                self.last_sync = time.monotonic()
                inputs = [dict(item, key=key) for key in snapshot["state"]["pending"]
                          if (item := await asyncio.to_thread(self.matrix.input, key))
                          and not snapshot["state"]["rooms"].get(item["room_id"], {}).get("gaps")]
                for item in inputs:
                    if item["intent"] == "message":
                        await self.control(item)
                if running and running.done():
                    try:
                        running.result()
                    except (Exception, asyncio.CancelledError) as error:
                        event("turn.failed", error_type=type(error).__name__, error=str(error))
                    running = None
                if not running:
                    self.config, self.role, matrix, self.model = load(self.path)
                    self.client.config = matrix
                    tasks = await asyncio.to_thread(self.recovery.discover)
                    if not gaps:
                        recovery_scanned()
                    for entry in tasks["tasks"]:
                        execution = entry["task"].get("execution") or {}
                        cp = execution.get("checkpoint") or {}
                        if execution.get("state") == "needs_verification" or (entry["task"]["status"] == "submitted" and cp.get("delivery") != "pending"):
                            continue
                        if entry["task"].get("room_id") in self.joined:
                            running = asyncio.create_task(self.process_task(tasks, entry, inputs))
                            break
                    if not running:
                        for item in inputs:
                            cp = (item.get("execution") or {}).get("checkpoint") or {}
                            if (item["intent"] == "message" and cp.get("delivery") != "delivered"
                                    and (item.get("execution") or {}).get("state") not in {"cancelled", "needs_verification"}
                                    and (cp.get("operation") or {}).get("state") not in {"started", "unknown"}):
                                running = asyncio.create_task(self.process_message(item))
                                break
                await asyncio.sleep(1)
            except Exception as error:
                event("runtime.pending", error_type=type(error).__name__)
                await asyncio.sleep(5)


def main():
    runtime = Runtime()
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            healthy = self.path == "/healthz" and time.monotonic() - runtime.last_sync < 30
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": healthy, "runtime": "qoder", "active": runtime.active is not None}).encode())
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("0.0.0.0", 8088), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asyncio.run(runtime.run())
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
