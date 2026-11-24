import asyncio
import unittest

from agentteams_qoder.questions import form_answer, form_questions
from test_interaction import runtime, source


class FormTests(unittest.TestCase):
    def test_native_types_and_multiple_options_are_preserved(self):
        schema = {"type": "object", "required": ["enabled", "count", "colors"], "properties": {
            "enabled": {"type": "boolean"}, "count": {"type": "integer", "minimum": 1},
            "colors": {"type": "array", "items": {"type": "string", "enum": ["red", "blue"]}},
            "note": {"type": "string"}}}
        questions = form_questions(schema)
        content = form_answer(schema, questions, [
            {"id": "enabled", "selected": ["1"]}, {"id": "count", "selected": [], "custom": "3"},
            {"id": "colors", "selected": ["0", "1"]}, {"id": "note", "selected": ["skip"]}])
        self.assertEqual(content, {"enabled": True, "count": 3, "colors": ["red", "blue"]})

    def test_invalid_native_form_values_do_not_resume_the_request(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "required": ["count"]}
        questions = form_questions(schema)
        for responses in ([{"id": "count", "custom": "0"}], [{"id": "other", "custom": "1"}],
                          [{"id": "count", "selected": ["unregistered"]}]):
            with self.assertRaises(ValueError):
                form_answer(schema, questions, responses)


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_question_reply_resumes_native_ask(self):
        instance = runtime()
        task = asyncio.create_task(instance.ask({"questions": [{"id": "color", "question": "选择颜色", "options": [{"id": "blue", "label": "蓝色"}]}]}))
        async with asyncio.timeout(1):
            while not instance.client.send_content.called:
                await asyncio.sleep(0)
        block = instance.client.send_content.call_args.args[1]["org.agentteams.run"]["blocks"][0]
        await instance.control(source("蓝色", **{"com.agentteams.answer": {
            "kind": "ask_user", "actor": "@human:test", "confirmation_id": block["confirmation_id"],
            "responses": [{"id": "color", "selected": ["blue"]}]}}))
        result = await asyncio.wait_for(task, 1)
        self.assertFalse(result["isError"])
        terminal = instance.client.send_content.call_args.args[1]["org.agentteams.run"]["blocks"][0]
        self.assertEqual(terminal["status"], "answered")
