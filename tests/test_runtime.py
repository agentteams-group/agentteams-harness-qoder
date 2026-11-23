import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from agentteams_qoder.runtime import Runtime


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime.__new__(Runtime)
        self.runtime.config = {"member": {"matrixUserId": "@worker:test"},
                               "team": {"admin": {"matrixUserId": "@admin:test"}}}
        self.runtime.matrix = Mock()
        self.runtime.matrix.message_claim.return_value = {"recovery": "resume", "execution": {"checkpoint": None}, "identity": {"event_key": "control"}}
        self.runtime.matrix.message_begin_tools.return_value = "operation"
        self.runtime.active = {"run": "$run", "room": "!room", "sender": "@human:test", "human": "@human:test",
                               "client": Mock(interrupt=AsyncMock(), query=AsyncMock()), "kind": "message",
                               "identity": {"event_key": "original"}, "cancelled": False}
        self.future = asyncio.get_running_loop().create_future()
        self.runtime.pending_approvals = {"request": ("$run", self.future)}

    def source(self, body, **kwargs):
        return {"key": "control", "event_id": "$control", "room_id": kwargs.get("room", "!room"),
                "input": {"sender": kwargs.get("sender", "@human:test"), "content": {"body": body}}}

    async def test_other_sender_and_room_cannot_approve(self):
        for source in (self.source("/approve request $run", sender="@other:test"),
                       self.source("/approve request $run", room="!other")):
            self.assertFalse(await self.runtime.control(source))
        self.assertFalse(self.future.done())
        self.runtime.matrix.message_claim.assert_not_called()

    async def test_stale_context_does_not_approve_active_request(self):
        self.assertTrue(await self.runtime.control(self.source("/approve request $old")))
        self.assertFalse(self.future.done())

    async def test_matching_context_approves_exact_request(self):
        self.assertTrue(await self.runtime.control(self.source("/approve request $run")))
        self.assertTrue(self.future.result())
        self.runtime.matrix.message_finish_tools.assert_called_once()

    async def test_cancel_persists_original_execution_before_native_interrupt(self):
        client = self.runtime.active["client"]
        self.assertTrue(await self.runtime.control(self.source("/cancel")))
        self.assertEqual(self.runtime.matrix.message_cancel.call_args_list[0].args[0], {"event_key": "original"})
        client.interrupt.assert_awaited_once()
        self.assertTrue(self.runtime.active["cancelled"])

    async def test_late_structured_stop_does_not_touch_new_run(self):
        source = self.source("stop")
        source["input"]["content"]["org.agentteams.execution_control"] = {
            "agent_id": "@worker:test", "action": "stop", "run_id": "$old"}
        self.assertFalse(await self.runtime.control(source))
        self.runtime.active["client"].interrupt.assert_not_awaited()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_reply_uses_delivery_only_path(self):
        runtime = Runtime.__new__(Runtime)
        runtime.matrix = Mock()
        runtime.matrix.message_claim.return_value = {"recovery": "deliver", "identity": {"event_key": "key"},
            "execution": {"checkpoint": {"result": "saved"}}, "saved_result": "saved response"}
        runtime.execute = AsyncMock()
        runtime.deliver = AsyncMock()
        await runtime.process_message({"key": "key", "room_id": "!room"})
        runtime.execute.assert_not_awaited()
        self.assertEqual(runtime.deliver.await_args.args[3], "saved response")
