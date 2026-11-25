"""Bind actual Qoder tools to the public Task bridge and operation journal."""
import asyncio
import json
from pathlib import Path

from .core import common


TASK_TOOL = "mcp__agentteams__task"
ASK_TOOL = "mcp__agentteams__ask_human"
READ_ONLY = frozenset({"Read", "Glob", "Grep"})
FILE_WRITES = frozenset({"Write", "Edit"})


class NativeTurn:
    def __init__(self, plugin, context, *, recovery, matrix, kind, identity, trace=None, ask=None):
        self.plugin, self.context = Path(plugin), context
        self.recovery, self.matrix, self.kind, self.identity = recovery, matrix, kind, identity
        self.operations = {}
        self.denied_before = set()
        self.complete = False
        self.completion = None
        self.terminal_call_id = None
        self.registered = False
        self.lock = asyncio.Lock()
        self.trace = trace
        self.ask, self.display = ask, None
        self.cancelled = False
        self.permission_denials = set()

    def admit(self):
        from task_operation import admit

        if self.cancelled:
            raise ValueError("This native turn was stopped")
        if self.complete:
            raise ValueError("The admitted Task phase is complete")
        if self.kind == "task":
            current = self.recovery.tools_current(self.identity)
        else:
            current = self.matrix.message_current(self.identity)
        admit(current["execution"].get("checkpoint"))
        result = common(self.plugin, "task_native").invoke(
            {"operation": "canonical_admit", "context": self.context})
        if result.get("ok") is not True:
            raise ValueError("Canonical Task outcome requires authoritative verification")
        return result

    async def task(self, arguments):
        async with self.lock:
            await asyncio.to_thread(self.admit)
            try:
                outcome = await asyncio.to_thread(common(self.plugin, "task_native").invoke,
                    {"operation": "task", "arguments": arguments, "context": self.context})
            except ValueError as error:
                if getattr(error, "effect", None) != "none":
                    raise
                outcome = {"result": {"ok": False, "effect": "none", "message": str(error)}}
            self.context.update(outcome.get("contextUpdate") or {})
            result = outcome.get("result", outcome)
            self.complete = outcome.get("phaseComplete") is True and bool(outcome.get("receiptId") and outcome.get("fact"))
            if self.complete:
                self.completion = outcome
            visible = result
            if self.complete:
                task = result.get("task") or {}
                visible = {"task": {key:task[key] for key in ("id","status","submissionId","completionId","planId") if key in task},
                           "receiptId":outcome["receiptId"]}
            parts = [{"type": "text", "text": json.dumps(visible, ensure_ascii=False)}]
            if outcome.get("fact"):
                parts.append({"type": "text", "text": outcome["fact"]})
            parts.extend({"type":"text","text":receipt["fact"]} for receipt in outcome.get("extraReceipts", []))
            return {"content": parts, "isError": result.get("ok") is False}

    def server(self):
        from qoder_agent_sdk import create_sdk_mcp_server, tool

        schema = common(self.plugin, "task_native").invoke(
            {"operation": "schema", "role": self.context["role"]})
        definition = tool("task", schema["description"], schema["inputSchema"])(self.task)
        tools = [definition]
        if self.ask and not any(self.context.get(key) for key in ("planning", "review", "completion")):
            tools.append(tool("ask_human", "Ask the authorized Human questions and wait for their actual answers.", {
                "type": "object", "required": ["questions"], "additionalProperties": False,
                "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 10, "items": {
                    "type": "object", "required": ["id", "question"], "additionalProperties": False,
                    "properties": {"id": {"type": "string", "minLength": 1}, "question": {"type": "string", "minLength": 1},
                        "multi_select": {"type": "boolean"}, "options": {"type": "array", "items": {
                            "type": "object", "required": ["id", "label"], "additionalProperties": False,
                            "properties": {"id": {"type": "string", "minLength": 1}, "label": {"type": "string", "minLength": 1}}}}}}}}})(self.ask))
        self.registered = True
        return create_sdk_mcp_server("agentteams", tools=tools)

    def before(self, name, arguments, call_id):
        if not call_id:
            raise ValueError("A native tool call ID is required")
        if name == TASK_TOOL and self.registered:
            self.admit()
            return {}
        if any(self.context.get(key) for key in ("planning", "review", "completion")):
            raise ValueError("This coordination phase permits only the registered public Task tool")
        if name == "Bash" and arguments.get("run_in_background"):
            raise ValueError("Background commands require an asynchronous terminal receipt adapter")
        if name in FILE_WRITES and self.context.get("execution"):
            prepared = self.context["context"]
            path = arguments.get("file_path")
            if not common(self.plugin, "task_context").task_write_path_allowed(
                    path, prepared["outputDirectory"], prepared["scratchDirectory"]):
                raise ValueError("Write the file inside this Task's outputDirectory or scratchDirectory")
        self.admit()
        call = {"id": call_id, "name": name, "arguments": arguments}
        if name in READ_ONLY or (name == ASK_TOOL and self.ask and self.registered):
            call["read_only"] = True
        call["pre_effect_error_codes"] = ["QODER_PERMISSION_DENIED"]
        if self.kind == "task":
            operation = self.recovery.begin_tools(self.identity, [call])
        else:
            operation = self.matrix.message_begin_tools(self.identity, [call])
        self.operations[call_id] = (name, operation)
        return {}

    def after(self, name, call_id, response, *, failed=False, interrupted=False):
        if call_id in self.permission_denials:
            return  # The SDK's exact PermissionDenied event already closed this operation.
        if call_id in self.denied_before:
            self.denied_before.remove(call_id)
            return  # Your own PreToolUse denial prevented this dispatch.
        if name == TASK_TOOL and self.registered:
            return
        if call_id not in self.operations or self.operations[call_id][0] != name:
            raise ValueError("Native terminal receipt does not match an admitted tool")
        if interrupted or (failed and name not in READ_ONLY):
            raise ValueError("Tool side effects are unknown; retain the operation for verification")
        if name == "Bash" and (not isinstance(response, dict) or response.get("kind") != "completed"
                or response.get("interrupted") is not False or type(response.get("exitCode")) is not int):
            raise ValueError("Bash has no native terminal receipt; retain the operation for verification")
        _, operation = self.operations[call_id]
        result = {"id": call_id, "content": response}
        if failed:
            result.update(status="error", effect="none")
        if self.kind == "task":
            self.recovery.finish_tools(self.identity, operation, [result])
        else:
            self.matrix.message_finish_tools(self.identity, operation, [result])
        del self.operations[call_id]

    def permission_denied(self, name, call_id, reason):
        if call_id in self.denied_before or (name == TASK_TOOL and self.registered):
            return
        if call_id in self.permission_denials:
            return
        if call_id not in self.operations or self.operations[call_id][0] != name:
            raise ValueError("Permission denial has no matching native admission")
        _, operation = self.operations[call_id]
        receipt = {"id": call_id, "content": reason, "status": "error", "effect": "none", "error_code": "QODER_PERMISSION_DENIED"}
        finish = self.recovery.finish_tools if self.kind == "task" else self.matrix.message_finish_tools
        finish(self.identity, operation, [receipt])
        del self.operations[call_id]
        self.permission_denials.add(call_id)

    async def hook(self, event, call_id, _options):
        async with self.lock:
            name = event.get("tool_name", "")
            call_id = call_id or event.get("tool_use_id")
            before = event["hook_event_name"] == "PreToolUse"
            denied = event["hook_event_name"] == "PermissionDenied"
            response = event.get("tool_response", event.get("error"))
            failed = (event["hook_event_name"] == "PostToolUseFailure"
                      or (isinstance(response, dict) and response.get("isError") is True))
            try:
                if before:
                    await asyncio.to_thread(self.before, name, event.get("tool_input") or {}, call_id)
                    if self.trace:
                        self.trace.tool_start(name, call_id)
                else:
                    if denied:
                        await asyncio.to_thread(self.permission_denied, name, call_id, event.get("denial_reason", "Permission denied"))
                    else:
                        await asyncio.to_thread(self.after, name, call_id,
                            response, failed=failed,
                            interrupted=event.get("is_interrupt") is True)
                    if self.trace:
                        self.trace.tool_end(call_id, denied or failed)
            except Exception as error:
                if not before:
                    if self.trace:
                        self.trace.tool_end(call_id, failed=True)
                    # Do not let the next inference/tool proceed on an unknown operation.
                    return {"continue_": False, "stopReason": "Native tool receipt requires verification: " + type(error).__name__}
                if call_id:
                    self.denied_before.add(call_id)
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "deny", "permissionDecisionReason": str(error)}}
            if self.display:
                await self.display.tool(name, call_id, event.get("tool_input"),
                    result=None if before else event.get("tool_response", event.get("error", event.get("denial_reason", "Completed"))),
                    failed=denied or failed, finished=not before)
            if name == TASK_TOOL and not before and not failed and self.complete:
                self.terminal_call_id = call_id
                return {"continue_": False, "stopReason": "Task phase completed"}
            return {}

    async def stop(self, _event, _call_id, _options):
        if self.cancelled:
            return {}
        if self.operations:
            return {"continue_": False, "stopReason": "An outstanding tool operation requires verification"}
        scoped = any(self.context.get(key) for key in ("execution", "planning", "review", "completion"))
        if scoped and not self.complete:
            await asyncio.to_thread(self.admit)
            return {"decision": "block", "reason": "Finish the admitted phase with your registered public Task tool."}
        return {}
