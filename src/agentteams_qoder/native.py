"""Bind actual Qoder tools to the public Task bridge and operation journal."""
import asyncio
import json
from pathlib import Path

from .core import common


TASK_TOOL = "mcp__agentteams__task"
READ_ONLY = frozenset({"Read", "Glob", "Grep"})
FILE_WRITES = frozenset({"Write", "Edit"})


class NativeTurn:
    def __init__(self, plugin, context, *, recovery, matrix, kind, identity, trace=None):
        self.plugin, self.context = Path(plugin), context
        self.recovery, self.matrix, self.kind, self.identity = recovery, matrix, kind, identity
        self.operations = {}
        self.denied_before = set()
        self.complete = False
        self.registered = False
        self.lock = asyncio.Lock()
        self.trace = trace

    def admit(self):
        from task_operation import admit

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
            self.complete = outcome.get("phaseComplete") is True
            parts = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
            if outcome.get("fact"):
                parts.append({"type": "text", "text": outcome["fact"]})
            return {"content": parts, "isError": result.get("ok") is False}

    def server(self):
        from qoder_agent_sdk import create_sdk_mcp_server, tool

        schema = common(self.plugin, "task_native").invoke(
            {"operation": "schema", "role": self.context["role"]})
        definition = tool("task", schema["description"], schema["inputSchema"])(self.task)
        self.registered = True
        return create_sdk_mcp_server("agentteams", tools=[definition])

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
        if name in READ_ONLY:
            call["read_only"] = True
        if self.kind == "task":
            operation = self.recovery.begin_tools(self.identity, [call])
        else:
            operation = self.matrix.message_begin_tools(self.identity, [call])
        self.operations[call_id] = (name, operation)
        return {}

    def after(self, name, call_id, response, *, failed=False, interrupted=False):
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

    async def hook(self, event, call_id, _options):
        async with self.lock:
            name = event.get("tool_name", "")
            call_id = call_id or event.get("tool_use_id")
            before = event["hook_event_name"] == "PreToolUse"
            try:
                if before:
                    await asyncio.to_thread(self.before, name, event.get("tool_input") or {}, call_id)
                    if self.trace:
                        self.trace.tool_start(name, call_id)
                else:
                    await asyncio.to_thread(self.after, name, call_id,
                        event.get("tool_response", event.get("error")),
                        failed=event["hook_event_name"] == "PostToolUseFailure",
                        interrupted=event.get("is_interrupt") is True)
                    if self.trace:
                        self.trace.tool_end(call_id, event["hook_event_name"] == "PostToolUseFailure")
                return {}
            except Exception as error:
                if not before:
                    if self.trace:
                        self.trace.tool_end(call_id, failed=True)
                    # Do not let the next inference/tool proceed on an unknown operation.
                    return {"continue_": False, "stop_reason": "Native tool receipt requires verification: " + type(error).__name__}
                if call_id:
                    self.denied_before.add(call_id)
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "deny", "permissionDecisionReason": str(error)}}

    async def stop(self, _event, _call_id, _options):
        if self.operations:
            return {"continue_": False, "stop_reason": "An outstanding tool operation requires verification"}
        scoped = any(self.context.get(key) for key in ("execution", "planning", "review", "completion"))
        if scoped and not self.complete:
            await asyncio.to_thread(self.admit)
            return {"decision": "block", "reason": "Finish the admitted phase with your registered public Task tool."}
        return {}
