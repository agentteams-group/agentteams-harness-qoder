import unittest
from unittest.mock import Mock

from qoder_agent_sdk import AssistantMessage, ThinkingBlock

from agentteams_qoder.display import TurnDisplay


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_terminal_edits_preserve_native_id_arguments_and_progress_flag(self):
        client = Mock()
        client.send_content.return_value = "$root"
        display = TurnDisplay(client, {"room_id": "!room", "event_id": "$source"})
        await display.tool("Read", "native-call", {"file_path": "fixture.txt"})
        await display.tool("Read", "native-call", result="file contents")
        initial = client.send_content.call_args_list[0].args[1]
        terminal = client.send_content.call_args_list[1].args[1]
        self.assertEqual(initial["org.agentteams.run"]["blocks"][0]["status"], "running")
        self.assertEqual(terminal["m.relates_to"], {"rel_type": "m.replace", "event_id": "$root"})
        block = terminal["m.new_content"]["org.agentteams.run"]["blocks"][0]
        self.assertEqual((block["tool_call_id"], block["status"]), ("native-call", "succeeded"))
        self.assertEqual(block["arguments"], {"file_path": "fixture.txt"})
        self.assertTrue(terminal["m.new_content"]["org.agentteams.progress_only"])

    async def test_thinking_uses_shared_blocks_and_can_be_suppressed(self):
        client = Mock()
        source = {"room_id": "!room", "event_id": "$source"}
        message = AssistantMessage([ThinkingBlock("Synthetic reasoning", "")], "fixture", message_id="message")
        await TurnDisplay(client, source, thinking=False).message(message)
        client.send_content.assert_not_called()
        await TurnDisplay(client, source).message(message)
        block = client.send_content.call_args.args[1]["org.agentteams.run"]["blocks"][0]
        self.assertEqual(block, {"type": "thinking", "content": "Synthetic reasoning", "isStreaming": False})
