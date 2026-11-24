import unittest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from qoder_agent_sdk import AssistantMessage, TextBlock

from agentteams_qoder.telemetry import NativeTrace


class TelemetryTests(unittest.TestCase):
    def test_native_ids_usage_and_content_exclusion(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace = NativeTrace(provider, role="worker", source={"event_id": "$source", "room_id": "!room", "input": {}},
                            identity={"generation": 3, "task_id": "task"}, session="session", context={})
        trace.model_start({})
        response = AssistantMessage(content=[TextBlock(text="private content")], model="actual-model", message_id="native-response", usage={"input_tokens": 12, "output_tokens": 4})
        trace.model_response(response)
        trace.model_response(response)
        trace.tool_start("Read", "native-call")
        trace.tool_end("native-call")
        trace.close()
        spans = exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        self.assertIn("worker-turn",spans[-1].attributes["langfuse.trace.tags"])
        self.assertIn("task:task",spans[-1].attributes["langfuse.trace.tags"])
        self.assertEqual(spans[-1].attributes["langfuse.session.id"],"session")
        self.assertTrue(spans[-1].attributes["langfuse.user.id"])
        self.assertEqual(spans[0].attributes["langfuse.observation.type"],"generation")
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)
        self.assertEqual(spans[0].attributes["gen_ai.response.model"], "actual-model")
        self.assertEqual(spans[0].attributes["gen_ai.usage.input_tokens"], 12)
        self.assertEqual(spans[1].attributes["gen_ai.tool.call.id"], "native-call")
        self.assertNotIn("private content", str([span.attributes for span in spans]))
        provider.shutdown()
