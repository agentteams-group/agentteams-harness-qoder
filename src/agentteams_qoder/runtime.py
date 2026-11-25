"""Qoder SDK execution with Controller-owned input, Task and reply recovery."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote
import uuid

from .config import authentication, external_mcp, gateway_options, instructions, load
from .core import common
from .native import ASK_TOOL, NativeTurn, TASK_TOOL
from .display import TurnDisplay
from .questions import form_answer, form_questions, validate_answers
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
        self.rooms_loaded = False
        self.active = None
        self.pending_requests = {}
        self.last_sync = 0
        self.gateway_ready, self.last_gateway_check = False, 0
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
        content = event.get("content") or {}
        body = content.get("body")
        verb = re.sub(r"^(?:@\S+\s+)+", "", body.strip()).partition(" ")[0] if isinstance(body, str) else ""
        control = ("com.agentteams.control" in content or "com.agentteams.answer" in content
                   or verb in {"/approve", "/reject", "/cancel", "/steer"})
        return ("message", None) if allowed(self.config, room, event, control=control) else None

    def observe_rooms(self, payload):
        from worker_matrix_raw import require_unencrypted_room

        if not self.rooms_loaded:
            self.joined = set(self.client.request("GET", "/_matrix/client/v3/joined_rooms")["joined_rooms"])
            self.rooms_loaded = True
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

        if not self.active or not context.tool_use_id:
            return PermissionResultDeny(message="No active native tool identity")
        native = self.active["native"]
        prepared = native.context.get("context") or {}
        if native.context.get("execution") and name in {"Read", "Glob", "Grep", "Write", "Edit"}:
            output = Path(prepared["outputDirectory"]).resolve()
            path = (output / (arguments.get("file_path") or arguments.get("path") or ".")).resolve()
            if name in {"Write", "Edit"}:
                approved = common(self.plugin,"task_context").task_write_path_allowed(str(path), str(output), prepared["scratchDirectory"])
            else:
                roots = [output, Path(prepared["scratchDirectory"]).resolve(), *[Path(i["directory"]).resolve() for i in prepared.get("inputs",[]) if i.get("readOnly") and i.get("directory")]]
                approved = any(path.is_relative_to(root) for root in roots)
            if approved:
                return PermissionResultAllow(updated_input=arguments)
        status, _ = await self.human_request("approval", context.tool_use_id, name,
            parameters=json.dumps(arguments, ensure_ascii=False),
            risk_details=getattr(context, "description", None) or getattr(context, "decision_reason", None) or "此工具需要你确认后执行",
            signal=getattr(context, "signal", None))
        if status != "approved":
            # SDK 1.0.17 does not emit PermissionDenied after a host denial.
            # This callback is the pre-dispatch gate; commit our own denial before returning it.
            async with native.lock:
                await asyncio.to_thread(native.permission_denied, name, context.tool_use_id, "Approval " + status)
                if native.trace:
                    native.trace.tool_end(context.tool_use_id, failed=True)
                if native.display:
                    await native.display.tool(name, context.tool_use_id, result="Approval " + status, failed=True, finished=True)
        return PermissionResultAllow(updated_input=arguments) if status == "approved" else PermissionResultDeny(message="Approval " + status)

    async def check_gateway(self):
        """Check the configured native model without sending an inference request."""
        from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient

        self.gateway_ready = False
        self.last_gateway_check = time.monotonic()
        gateway = gateway_options(self.model)
        options = QoderAgentOptions(auth=authentication(), cwd=self.home / "workspace", tools=[], setting_sources=[],
            model=gateway["model"], settings=gateway["settings"],
            env={**gateway["env"], "QODER_CONFIG_DIR": str(self.home / ".qoder")})
        async with QoderSDKClient(options=options) as client:
            models = await client.get_available_models()
            if not any(model.get("value") == gateway["model"] and model.get("isEnabled") for model in models):
                raise ValueError("Qoder did not enable the projected gateway model")
        self.gateway_ready = True

    async def human_request(self, kind, native_id, tool, *, parameters="", risk_details="", questions=None, validator=None, signal=None):
        active = self.active
        admin = self.config.get("team", {}).get("admin", {}).get("matrixUserId")
        if not active or active["cancelled"] or not (active.get("human") or admin):
            return "cancelled", None
        context_id = uuid.uuid5(uuid.NAMESPACE_URL, active["run"] + ":" + str(active["identity"]) + ":" + active.get("session", "")).hex
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, context_id + ":" + native_id).hex
        future = asyncio.get_running_loop().create_future()
        entry = {"run": active["run"], "room": active["room"], "context": context_id,
                 "kind": kind, "future": future, "validator": validator}
        self.pending_requests[request_id] = entry
        metadata = {"version": 1, "runtime": "qoder", "request_id": request_id,
                    "reply_format": "com.agentteams.answer", "context_id": context_id,
                    "status": "pending", "tool_name": tool, "parameters": parameters, "risk_details": risk_details,
                    "expires_at": int((time.time() + 300) * 1000)}
        question = {"type": "confirmation", "confirmation_id": request_id, "tool_name": tool,
                    "reply_format": "com.agentteams.answer", "status": "pending", "questions": questions,
                    "requested_of": active.get("human") or admin, "expires_at": metadata["expires_at"]}
        status, value, sent, cancelled = "cancelled", None, False, None
        async def publish(state):
            content = {"msgtype": "m.text", "body": ("请确认工具操作：" + tool if kind == "approval" else "请回答：" + tool)
                       if state == "pending" else "人工请求已处理：" + state, "org.agentteams.progress_only": True}
            if kind == "approval":
                content["org.agentteams.approval"] = {**metadata, "status": state}
            else:
                content["org.agentteams.run"] = {"version": "1", "run_id": active["run"], "blocks": [{**question, "status": "cancelled" if state == "invalid" else state}]}
            await asyncio.to_thread(self.client.send_content, active["room"], content, "qoder:request:" + request_id + ":" + state)
        try:
            await publish("pending")
            sent = True
            if signal:
                cancelled = asyncio.create_task(signal.wait())
            done, _ = await asyncio.wait([future, *([cancelled] if cancelled else [])], timeout=300,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancelled and cancelled in done:
                status = "cancelled"
            elif future in done:
                status, value = future.result()
            else:
                status = "expired"
            return status, value
        finally:
            self.pending_requests.pop(request_id, None)
            if cancelled:
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
            if not future.done():
                future.cancel()
            if sent:
                await publish(status)

    async def ask(self, arguments):
        questions = arguments["questions"]
        if not questions or len({q["id"] for q in questions}) != len(questions):
            raise ValueError("Question IDs must be unique")
        status, answers = await self.human_request("question", uuid.uuid4().hex, "ask_human", questions=questions,
            validator=lambda responses: validate_answers(questions, responses))
        return {"content": [{"type": "text", "text": json.dumps({"status": status, "answers": answers}, ensure_ascii=False)}],
                "isError": status != "answered"}

    async def elicitation(self, request):
        if request.get("mode", "form") != "form":
            return {"action": "cancel"}  # URL authorization is not a form answer.
        schema = request.get("requestedSchema") or {}
        questions = form_questions(schema)
        while True:
            status, content = await self.human_request("question", uuid.uuid4().hex,
                request.get("message") or request["serverName"], questions=questions,
                validator=lambda responses: form_answer(schema, questions, responses))
            if status != "invalid":
                return {"action": "accept", "content": content} if status == "answered" else {"action": "cancel"}

    async def retire_control(self, item, *, operation=None, identity=None, result="Control ignored"):
        if identity is None:
            claim = await asyncio.to_thread(self.matrix.message_claim, item["key"])
            if claim["recovery"] != "resume":
                return
            identity = claim["identity"]
        if operation:
            await asyncio.to_thread(self.matrix.message_finish_tools, identity, operation,
                                    [{"id": item["event_id"], "content": result}])
        await asyncio.to_thread(self.matrix.message_cancel, identity)

    async def control(self, item):
        from task_operation import admit

        content = item["input"]["content"]
        text = re.sub(r"^(?:@\S+\s+)+", "", content["body"].strip())
        verb, _, argument = text.partition(" ")
        wire, answer = content.get("com.agentteams.control"), content.get("com.agentteams.answer")
        if wire is not None:
            if (not isinstance(wire, dict) or wire.get("action") not in {"stop", "steer"}
                    or wire.get("agent_id") != self.config["member"]["matrixUserId"]):
                await self.retire_control(item)
                return True
            if not self.active or wire.get("run_id") != self.active["run"]:
                if wire.get("action") == "steer":
                    return False  # Preserve late supplements as ordinary follow-up input.
                await self.retire_control(item)
                return True
            verb = {"stop": "/cancel", "steer": "/steer"}.get(wire.get("action"), "")
            argument = text
        if answer is not None:
            verb = "/answer"
        if verb not in {"/cancel", "/steer", "/approve", "/reject", "/answer"}:
            return False
        if verb == "/answer" and answer is None:
            await self.retire_control(item)
            return True
        active = self.active
        sender = item["input"]["sender"]
        admin = self.config.get("team", {}).get("admin", {}).get("matrixUserId")
        authorized = active and item["room_id"] == active["room"] and sender in {active["sender"], admin}
        # Agent-originated turns may only be controlled by the Human admin.
        if authorized and sender != admin:
            authorized = sender == self.config["member"].get("adminMatrixUserId") or sender == active.get("human")
        if not authorized:
            await self.retire_control(item)
            return True
        if answer is not None or verb in {"/approve", "/reject"}:
            request_id, _, context_id = argument.strip().partition(" ")
            if answer is not None:
                if not isinstance(answer, dict) or answer.get("actor", sender) != sender:
                    await self.retire_control(item)
                    return True
                request_id, context_id = answer.get("confirmation_id"), answer.get("context_id")
            if not isinstance(request_id, str):
                await self.retire_control(item)
                return True
            pending = self.pending_requests.get(request_id)
            valid = (pending and pending["run"] == active["run"] and pending["room"] == item["room_id"]
                     and not pending["future"].done())
            if valid:
                try:
                    if pending["kind"] == "approval":
                        if context_id != pending["context"]:
                            raise ValueError("Approval context differs")
                        status = answer.get("answer") if answer is not None else "approved" if verb == "/approve" else "rejected"
                        if status not in {"approved", "rejected", "cancelled"}:
                            raise ValueError("Invalid approval choice")
                        result = None
                    else:
                        if not isinstance(answer, dict) or answer.get("kind") != "ask_user":
                            raise ValueError("Structured question answers required")
                        result = pending["validator"](answer.get("responses"))
                        status = "answered"
                except (ValueError, TypeError, KeyError, IndexError):
                    if pending["kind"] == "question":
                        pending["future"].set_result(("invalid", None))
                    await self.retire_control(item)
                    return True
                pending["future"].set_result((status, result))
            await self.retire_control(item)
            return True
        if not active.get("direct") or active["cancelled"]:
            await self.retire_control(item)
            return True
        async with active["lock"]:
            if self.active is not active or not active["accepting"] or active["native"].complete:
                if verb == "/steer":
                    return False
                await self.retire_control(item)
                return True
            prompt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, item["event_id"]))
            if prompt_id in active["inputs"]:
                return True
            claim = await asyncio.to_thread(self.matrix.message_claim, item["key"])
            if claim["recovery"] != "resume":
                return True
            admit(claim["execution"].get("checkpoint"))
            identity = claim["identity"]
            call = {"id": item["event_id"], "name": "qoder.control", "arguments": {"verb": verb, "run": active["run"], "input_uuid": prompt_id}}
            operation = await asyncio.to_thread(self.matrix.message_begin_tools, identity, [call])
            entry = {"source": item, "identity": identity, "operation": operation, "committed": False,
                     "consumed": False, "stop": verb == "/cancel", "text": argument.strip()}
            active["inputs"][prompt_id] = entry
            if verb == "/cancel":
                active["cancelled"] = True
                active["native"].cancelled = True
                for pending in self.pending_requests.values():
                    if not pending["future"].done():
                        pending["future"].set_result(("cancelled", None))
                await active["display"].status("stopping")
                await active["client"].interrupt()
            elif argument.strip():
                await active["client"].query(argument.strip(), priority="next", message_uuid=prompt_id)
            return True

    def prepare(self, source, *, recovered=None):
        from task_coordination import notification

        context = {"role": self.role, "sourceEvent": source["event_id"], "sources": [],
                   "actor": self.config["member"]["matrixUserId"]}
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
            text += "\n\n" + prepared.get("fact", "") + "\n" + json.dumps(common(self.plugin,"task_context").model_context(prepared.get("context") or {}), ensure_ascii=False)
        return context, text

    async def execute(self, source, identity, *, recovered=None):
        from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, HookMatcher, ResultMessage, AssistantMessage, UserMessage
        from worker_sessions import WorkerSessions

        gateway = gateway_options(self.model)
        context, text = await asyncio.to_thread(self.prepare, source, recovered=recovered)
        kind = "task" if recovered else "message"
        native = NativeTurn(self.plugin, context, recovery=self.recovery, matrix=self.matrix, kind=kind, identity=identity, ask=self.ask)
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
        from opentelemetry.context import attach, detach
        trace_token = attach(trace.parent) if trace.parent else None
        peers = {member.get("matrixUserId") for member in self.config.get("team", {}).get("members", [])}
        requester = source["input"]["sender"]
        human = requester if source["room_id"] == self.config["member"].get("personalRoomId") and requester not in peers else None
        scoped = any(context.get(key) for key in ("planning", "execution", "review", "completion"))
        direct = kind == "message" and human is not None and not scoped
        policy = self.config.get("desired", {}).get("channels", {}).get("matrix", {})
        display = TurnDisplay(self.client, source, direct=direct, thinking=not scoped and policy.get("show_thinking", True),
                              tools=not policy.get("filter_tool_messages", False))
        native.display = display
        active = {"run": source["event_id"], "room": source["room_id"], "sender": requester,
                  "human": human, "cancelled": False, "kind": kind, "identity": identity,
                  "session": session_id, "direct": direct, "display": display, "native": native,
                  "lock": asyncio.Lock(), "inputs": {}, "accepting": True}
        prompt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source["event_id"]))
        persisted = asyncio.Event()

        async def committed(key, rows):
            if key.get("subpath"):
                raise ValueError("Nested transcripts are not supported by this candidate")
            for row in rows:
                queued = row.get("attachment") if row.get("type") == "attachment" else None
                queued = queued if isinstance(queued, dict) and queued.get("type") == "queued_command" and not queued.get("isMeta") else None
                native_id = queued.get("source_uuid") if queued else row.get("uuid")
                entry = active["inputs"].get(native_id)
                item = source if native_id == prompt_id else (entry or {}).get("source")
                if (row.get("type") == "user" or queued) and item:
                    if queued and entry:
                        prompt = queued.get("prompt")
                        if isinstance(prompt, list) and all(isinstance(part, dict) and part.get("type") == "text" for part in prompt):
                            prompt = "".join(part["text"] for part in prompt)
                        if prompt != entry["text"]:
                            raise ValueError("Queued native input differs from the accepted supplement")
                    if item.get("key"):
                        snapshot = await asyncio.to_thread(self.matrix.native_input, item["key"])
                        digest = hashlib.sha256(self.store.encode(row).encode()).hexdigest()
                        await asyncio.to_thread(self.matrix.native_committed, item["key"], snapshot,
                                                session_id + ":" + native_id, digest)
                    if native_id == prompt_id:
                        persisted.set()
                    elif entry:
                        entry["committed"] = True
                        trace.input_committed(item["event_id"])

        async def resolve_model(_context):
            trace.model_start(_context)
            return {"model": gateway["model"]}

        tools = [] if any(context.get(key) for key in ("planning", "review", "completion")) else ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        answer = None
        failed = True
        try:
            options = QoderAgentOptions(cwd=(context.get("context") or {}).get("outputDirectory") or str(self.home / "workspace"),
                system_prompt=instructions(self.config, self.role, self.plugin, self.home) + "\nYour public Task tool is " + TASK_TOOL,
                tools=tools, mcp_servers={"agentteams": native.server(), **(external_mcp(self.config, self.model["api_key"]) if tools else {})},
                allowed_tools=[TASK_TOOL, ASK_TOOL], disallowed_tools=["Task", "Agent", "TaskOutput", "TaskStop"],
                strict_mcp_config=True, setting_sources=[], permission_mode="default", auth=authentication(),
                can_use_tool=self.permission, on_elicitation=self.elicitation, include_partial_messages=True,
                hooks={name: [HookMatcher(hooks=[native.hook])] for name in
                       ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied")} | {"Stop": [HookMatcher(hooks=[native.stop])]},
                resolve_model=resolve_model, session_store=self.store, session_store_flush="eager",
                model=gateway["model"], settings=gateway["settings"],
                env={**gateway["env"], "QODER_CONFIG_DIR": str(self.home / ".qoder")},
                resume=resume, session_id=None if resume else session_id, max_turns=50)
            self.store.committed = committed
            async with QoderSDKClient(options=options) as client:
                models = await client.get_available_models()
                if not any(model.get("value") == gateway["model"] and model.get("isEnabled") for model in models):
                    raise RuntimeError("Qoder did not enable the projected OpenAI-compatible gateway model; "
                                       "check Qoder SDK custom-endpoint support and account permissions")
                self.gateway_ready = True
                active["client"] = client
                self.active = active
                status = await client.get_mcp_status()
                registered = [tool for server in status.get("mcpServers", []) if server.get("name") == "agentteams"
                              for tool in server.get("tools", [])]
                if not any(tool.get("name") in {"task", TASK_TOOL} for tool in registered):
                    raise ValueError("Qoder did not discover the registered public Task tool")
                await display.status("running")
                await client.query(text, message_uuid=prompt_id)
                async for message in client.receive_messages():
                    await display.message(message)
                    if isinstance(message, UserMessage) and message.uuid in active["inputs"]:
                        active["inputs"][message.uuid]["consumed"] = True
                    if isinstance(message, AssistantMessage):
                        trace.model_response(message)
                    if isinstance(message, ResultMessage):
                        async with active["lock"]:
                            if active["cancelled"]:
                                active["accepting"] = False
                                break
                            if message.is_error and not native.complete:
                                raise RuntimeError("Qoder turn failed: " + "; ".join(message.errors or [str(message.subtype)]))
                            if any(not entry["consumed"] for entry in active["inputs"].values()):
                                continue  # An accepted supplement belongs to a following native response.
                            active["accepting"] = False
                            answer = ((native.completion.get("displayText") or native.completion["fact"])
                                      if native.complete else message.result or "")
                            break
                if active["cancelled"]:
                    for entry in active["inputs"].values():
                        if entry["stop"]:
                            await self.retire_control(entry["source"], operation=entry["operation"], identity=entry["identity"], result="Native interruption reached its terminal response")
                    if native.operations:
                        await display.status("unavailable")
                        raise ValueError("An interrupted tool requires authoritative verification")
                    await asyncio.to_thread(self.matrix.message_cancel, identity)
                    await display.status("stopped")
                    failed = False
                    raise asyncio.CancelledError()
                if answer is None:
                    raise ValueError("Qoder stream ended without a terminal response")
                view = await asyncio.to_thread(sessions.load, work)
            if not persisted.is_set():
                raise ValueError("Qoder did not persist the exact native user input")
            if native.operations:
                raise ValueError("Qoder ended with an unverified native tool operation")
            if native.complete and not self.store.has_task_receipt(session_id, native.terminal_call_id, native.completion["receiptId"]):
                raise ValueError("Qoder did not persist its native terminal Task receipt")
            if any(context.get(key) for key in ("execution", "planning", "review", "completion")) and not native.complete:
                raise ValueError("Qoder ended without a confirmed public Task receipt")
            for entry in active["inputs"].values():
                if not entry["committed"] or not entry["consumed"]:
                    raise ValueError("Qoder did not commit and consume the accepted supplement")
                await self.retire_control(entry["source"], operation=entry["operation"], identity=entry["identity"], result="Supplement committed and consumed in the native session")
            # Retain native cache only for the snapshot actually represented at
            # input admission. Changed Task checkpoints cause a fresh session.
            if view["config_generation"] == config_generation:
                await asyncio.to_thread(sessions.remember, work, view, identity, session_id,
                                        self.store.digest(session_id), int(time.time()) + 86400)
            failed = False
            return answer
        finally:
            active["accepting"] = False
            try:
                await display.close(failed=failed)
                if failed:
                    await display.status("unavailable" if native.operations else "failed")
            finally:
                try:
                    trace.close()
                finally:
                    if trace_token is not None:
                        detach(trace_token)
                    self.store.committed = None
                    self.active = None
                    for pending in self.pending_requests.values():
                        if not pending["future"].done():
                            pending["future"].set_result(("cancelled", None))

    async def deliver(self, room, identity, execution, text, *, task=False, reply_to=None, run=None):
        from worker_matrix_delivery import confirm_bound
        from worker_matrix_raw import publish_raw_saved

        def bind(checkpoint):
            if task:
                saved = self.recovery.checkpoint(identity["task_id"], identity["team_id"], checkpoint, expected_identity=identity)
            else:
                saved = self.matrix.message_checkpoint(identity, checkpoint)
            confirm_bound(saved["execution"], identity, "qoder", checkpoint)
        content = {"org.agentteams.progress_only": True} if task else {}
        if task:
            run = run or "recovery:" + identity["task_id"] + ":" + str(identity["generation"])
            content.update({"org.agentteams.run_id": run,
                "org.agentteams.run": {"version": "1", "run_id": run, "blocks": [{"type": "text", "text": text}]}})
        if reply_to and reply_to.startswith("$"):
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        event = await asyncio.to_thread(publish_raw_saved, self.client, room, text, execution["checkpoint"],
                                        self.config["member"]["matrixUserId"], bind,
                                        content=content)
        if task:
            await asyncio.to_thread(self.recovery.delivered, identity["task_id"], identity["team_id"], event)
        else:
            await asyncio.to_thread(self.matrix.message_delivered, identity, event)
        return event

    async def process_message(self, source):
        claimed = await asyncio.to_thread(self.matrix.message_claim, source["key"])
        event = None
        if claimed["recovery"] == "resume" and "org.agentteams.coordination" not in source["input"]["content"] and not self.classify(source["room_id"],{**source["input"],"event_id":source["event_id"]}):
            await asyncio.to_thread(self.matrix.message_cancel,claimed["identity"])
            return
        if claimed["recovery"] == "deliver":
            event = await self.deliver(source["room_id"], claimed["identity"], claimed["execution"], claimed["saved_result"], reply_to=source["event_id"])
        elif claimed["recovery"] == "resume":
            if claimed.get("resume_text") and claimed["execution"].get("checkpoint"):
                source = {**source, "input": {**source["input"], "content": {
                    **source["input"]["content"], "body": claimed["resume_text"]}}}
            answer = await self.execute(source, claimed["identity"])
            saved = await asyncio.to_thread(self.matrix.message_save_reply, claimed["identity"], answer)
            event = await self.deliver(source["room_id"], claimed["identity"], saved["execution"], answer, reply_to=source["event_id"])
        if event:
            direct = (source["room_id"] == self.config["member"].get("personalRoomId")
                      and "org.agentteams.coordination" not in source["input"]["content"])
            await TurnDisplay(self.client, source, direct=direct).status("completed", event)

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
        await self.deliver(room, identity, saved["execution"], answer, task=True, run=source["event_id"])

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
                consumed = set()
                for item in inputs:
                    if item["intent"] == "message" and await self.control(item):
                        consumed.add(item["key"])
                inputs = [item for item in inputs if item["key"] not in consumed]
                if running and running.done():
                    try:
                        running.result()
                    except (Exception, asyncio.CancelledError) as error:
                        event("turn.failed", error_type=type(error).__name__, error=str(error))
                    running = None
                if not running:
                    previous_model = self.model
                    self.config, self.role, matrix, self.model = load(self.path)
                    self.client.config = matrix
                    if previous_model != self.model:
                        self.gateway_ready, self.last_gateway_check = False, 0
                    if not self.gateway_ready and time.monotonic() - self.last_gateway_check >= 30:
                        try:
                            await self.check_gateway()
                        except Exception as error:
                            event("model.unavailable", error_type=type(error).__name__)
                    tasks = await asyncio.to_thread(self.recovery.discover)
                    if not gaps:
                        recovery_scanned()
                    for entry in tasks["tasks"]:
                        execution = entry["task"].get("execution") or {}
                        cp = execution.get("checkpoint") or {}
                        if execution.get("state") == "needs_verification" or (entry["task"]["status"] == "submitted" and cp.get("delivery") != "pending"):
                            continue
                        if not self.gateway_ready and cp.get("delivery") != "pending":
                            continue
                        if entry["task"].get("room_id") in self.joined:
                            running = asyncio.create_task(self.process_task(tasks, entry, inputs))
                            break
                    if not running:
                        for item in inputs:
                            cp = (item.get("execution") or {}).get("checkpoint") or {}
                            if not self.gateway_ready and cp.get("delivery") != "pending":
                                continue
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
            healthy = self.path == "/healthz" and time.monotonic() - runtime.last_sync < 30 and runtime.gateway_ready
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": healthy, "runtime": "qoder", "active": runtime.active is not None,
                                        "model_ready": runtime.gateway_ready}).encode())
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
