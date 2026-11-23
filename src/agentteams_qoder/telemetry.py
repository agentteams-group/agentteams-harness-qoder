"""Emit native SDK response/tool facts without collecting prompt or tool bodies."""
import json
import os
import time


def configure():
    if os.getenv("AGENTTEAMS_OTEL_ENABLED", "").lower() not in {"true", "1"}:
        return None
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": "agentteams-worker",
        "agentteams.runtime": "qoder", "agentteams.worker.name": os.environ["AGENTTEAMS_WORKER_NAME"]}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


class NativeTrace:
    def __init__(self, provider, *, role, source, identity, session, context):
        self.provider = provider
        self.tracer = provider.get_tracer("agentteams.qoder") if provider else None
        self.attributes = {"agentteams.runtime": "qoder", "agentteams.role": role,
            "agentteams.session.id": session, "agentteams.turn.id": source["event_id"],
            "agentteams.room.id": source["room_id"], "agentteams.telemetry.capture_content": False,
            "agentteams.execution.generation": identity["generation"],
            "agentteams.input.event_ids": json.dumps([source["event_id"]] if source["event_id"].startswith("$") else [])}
        if identity.get("task_id"):
            self.attributes["agentteams.task.id"] = identity["task_id"]
        coordination = (source["input"].get("content", {}).get("org.agentteams.coordination") or {}).get("channel_meta", {}).get("teamharness_coordination") or {}
        for key in ("id", "kind"):
            if coordination.get(key):
                self.attributes["agentteams.coordination." + key] = coordination[key]
        self.tools, self.messages = {}, set()
        self.model_started = None
        self.root = None
        self.parent = None
        if self.tracer:
            from opentelemetry.trace import set_span_in_context
            from opentelemetry.propagate import extract
            carrier = (source["input"].get("content", {}).get("org.agentteams.coordination") or {}).get("channel_meta", {}).get("otel_carrier") or {}
            self.root = self.tracer.start_span("qoder.turn", context=extract(carrier),
                attributes={**self.attributes, "gen_ai.operation.name": "invoke_agent"})
            self.parent = set_span_in_context(self.root)

    def model_start(self, _context):
        self.model_started = time.time_ns()

    def model_response(self, message):
        identity = message.message_id or message.uuid
        if not self.tracer or not identity or identity in self.messages:
            return
        self.messages.add(identity)
        fields = {**self.attributes, "gen_ai.operation.name": "chat", "gen_ai.provider.name": "qoder",
                  "gen_ai.response.model": message.model, "gen_ai.response.id": identity}
        usage = message.usage or {}
        for key in ("input_tokens", "output_tokens"):
            if type(usage.get(key)) is int:
                fields["gen_ai.usage." + key] = usage[key]
        if self.model_started is None:
            fields["agentteams.model.duration_unknown"] = True
        span = self.tracer.start_span("qoder.model.response", context=self.parent, attributes=fields, start_time=self.model_started)
        span.end()
        self.model_started = None

    def tool_start(self, name, identity):
        if self.tracer:
            self.tools[identity] = self.tracer.start_span("qoder.tool", context=self.parent, attributes={**self.attributes,
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": identity})

    def tool_end(self, identity, failed=False):
        span = self.tools.pop(identity, None)
        if span:
            if failed:
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR))
            span.end()

    def close(self):
        for identity in list(self.tools):
            self.tool_end(identity, failed=True)
        if self.root:
            self.root.end()
        if self.provider:
            self.provider.force_flush(timeout_millis=5000)
