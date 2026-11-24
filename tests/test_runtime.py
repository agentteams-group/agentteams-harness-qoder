import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agentteams_qoder.runtime import Runtime
from test_config import record


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_requires_the_actual_gateway_model_without_running_inference(self):
        from qoder_agent_sdk import qodercli_auth

        runtime = Runtime.__new__(Runtime)
        runtime.home = Path("/tmp/qoder-readiness")
        runtime.model = {"model": "fixture", "url": "https://gateway/v1", "api_key": "synthetic"}
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_available_models = AsyncMock(return_value=[])
        with patch("qoder_agent_sdk.QoderSDKClient", return_value=client), \
                patch("agentteams_qoder.runtime.authentication", return_value=qodercli_auth()):
            with self.assertRaisesRegex(ValueError, "gateway model"):
                await runtime.check_gateway()
            self.assertFalse(runtime.gateway_ready)
            client.get_available_models.return_value = [{"value": "agentteams-gateway/fixture", "isEnabled": True}]
            await runtime.check_gateway()
            self.assertTrue(runtime.gateway_ready)
            client.query.assert_not_called()

    async def test_runtime_routes_every_model_policy_purpose_to_direct_gateway(self):
        from qoder_agent_sdk import qodercli_auth
        from qoder_agent_sdk._internal.query import Query

        runtime = Runtime.__new__(Runtime)
        runtime.home = Path("/tmp/qoder-member-fixture")
        runtime.plugin = Path(os.environ["AT_SOURCE"]) / "plugins/teamharness"
        runtime.config, runtime.role = record(), "worker"
        runtime.model = {"model": "gateway-model", "url": "https://gateway/v1", "api_key": "fixture-gateway"}
        runtime.matrix, runtime.recovery, runtime.store = Mock(), Mock(), Mock()
        runtime.telemetry, runtime.pending_requests = None, {}
        runtime.client = Mock()
        runtime.prepare = Mock(return_value=({"role": "worker"}, "fixture"))
        client = Mock()
        client.return_value.__aenter__ = AsyncMock(return_value=client.return_value)
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        client.return_value.get_available_models = AsyncMock(return_value=[])
        client.return_value.query = AsyncMock()
        with patch("worker_sessions.WorkerSessions.load", return_value={"config_generation": 1}), \
                patch("worker_sessions.WorkerSessions.usable", return_value=False), \
                patch("agentteams_qoder.native.NativeTurn.admit"), \
                patch("agentteams_qoder.runtime.instructions", return_value="fixture"), \
                patch("agentteams_qoder.runtime.authentication", return_value=qodercli_auth()), \
                patch("qoder_agent_sdk.QoderSDKClient", client):
            with self.assertRaisesRegex(RuntimeError, "OpenAI-compatible gateway"):
                await runtime.execute({"event_id": "fixture", "room_id": "!dm:test", "input": {"sender": "@human:test", "content": {}}},
                                      {"event_key": "fixture", "generation": 1})
        options = client.call_args.kwargs["options"]
        client.return_value.query.assert_not_awaited()
        self.assertEqual(options.model, "agentteams-gateway/gateway-model")
        self.assertEqual(options.settings["providers"]["agentteams-gateway"]["baseUrl"], "https://gateway/v1")
        self.assertEqual(options.env["QODER_SDK_CUSTOM_BASE_URL_BYOK"], "1")
        self.assertEqual(options.env["QODER_CONFIG_DIR"], str(runtime.home / ".qoder"))
        transport = Mock(write=AsyncMock())
        query = Query(transport, is_streaming_mode=True, resolve_model=options.resolve_model)
        self.addCleanup(query._message_send.close)
        self.addCleanup(query._message_receive.close)
        for purpose in ("main", "compact", "tool_summary"):
            await query._handle_control_request({"request_id": purpose,
                "request": {"subtype": "get_model_policy", "purpose": purpose}})
            response = json.loads(transport.write.await_args.args[0])["response"]["response"]
            self.assertEqual(response["model"], options.model)
            self.assertNotIn("custom_model", response)
            self.assertNotIn("fixture-gateway", json.dumps(response))


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
        self.runtime.active.update(direct=True, accepting=True, lock=asyncio.Lock(), inputs={},
                                   native=Mock(complete=False), display=Mock(status=AsyncMock()))
        self.runtime.pending_requests = {"request": {"run": "$run", "room": "!room", "context": "ctx",
                                                    "future": self.future, "kind": "approval"}}

    def source(self, body, **kwargs):
        return {"key": "control", "event_id": "$control", "room_id": kwargs.get("room", "!room"),
                "input": {"sender": kwargs.get("sender", "@human:test"), "content": {"body": body}}}

    async def test_other_sender_and_room_cannot_approve(self):
        for source in (self.source("/approve request $run", sender="@other:test"),
                       self.source("/approve request $run", room="!other")):
            self.assertTrue(await self.runtime.control(source))
        self.assertFalse(self.future.done())
        self.assertFalse(self.future.done())

    async def test_stale_context_does_not_approve_active_request(self):
        self.assertTrue(await self.runtime.control(self.source("/approve request $old")))
        self.assertFalse(self.future.done())

    async def test_matching_context_approves_exact_request(self):
        self.assertTrue(await self.runtime.control(self.source("@worker /approve request ctx")))
        self.assertEqual(self.future.result(), ("approved", None))
        self.runtime.matrix.message_cancel.assert_called_once()

    async def test_cancel_persists_control_intent_before_native_interrupt(self):
        client = self.runtime.active["client"]
        self.assertTrue(await self.runtime.control(self.source("/cancel")))
        self.runtime.matrix.message_begin_tools.assert_called_once()
        self.runtime.matrix.message_cancel.assert_not_called()
        client.interrupt.assert_awaited_once()
        self.assertTrue(self.runtime.active["cancelled"])

    async def test_late_structured_stop_does_not_touch_new_run(self):
        source = self.source("stop")
        source["input"]["content"]["com.agentteams.control"] = {
            "agent_id": "@worker:test", "action": "stop", "run_id": "$old"}
        self.assertTrue(await self.runtime.control(source))
        self.runtime.active["client"].interrupt.assert_not_awaited()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_reply_uses_delivery_only_path(self):
        runtime = Runtime.__new__(Runtime)
        runtime.matrix = Mock()
        runtime.matrix.message_claim.return_value = {"recovery": "deliver", "identity": {"event_key": "key"},
            "execution": {"checkpoint": {"result": "saved"}}, "saved_result": "saved response"}
        runtime.execute = AsyncMock()
        runtime.deliver = AsyncMock()
        runtime.deliver.return_value = None
        await runtime.process_message({"key": "key", "event_id": "$source", "room_id": "!room"})
        runtime.execute.assert_not_awaited()
        self.assertEqual(runtime.deliver.await_args.args[3], "saved response")
