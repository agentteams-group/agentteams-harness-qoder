import asyncio
import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agentteams_qoder.runtime import Runtime
from agentteams_qoder.transport import MatrixClient


def source(body, **extra):
    return {"key": "control", "event_id": "$control", "room_id": "!room", "input": {
        "sender": "@human:test", "content": {"msgtype": "m.text", "body": body, **extra}}}


def runtime():
    instance = Runtime.__new__(Runtime)
    instance.config = {"member": {"matrixUserId": "@worker:test", "personalRoomId": "!room"},
                       "team": {"admin": {"matrixUserId": "@admin:test"}}}
    instance.matrix, instance.client, instance.store = Mock(), Mock(), Mock()
    instance.matrix.message_claim.return_value = {"recovery": "resume", "execution": {"checkpoint": None},
                                                "identity": {"event_key": "control"}}
    instance.matrix.message_begin_tools.return_value = "operation"
    instance.pending_requests = {}
    instance.active = {"run": "$run", "room": "!room", "sender": "@human:test", "human": "@human:test",
        "client": Mock(interrupt=AsyncMock(), query=AsyncMock()), "kind": "message", "direct": True,
        "identity": {"event_key": "original"}, "cancelled": False, "accepting": True,
        "lock": asyncio.Lock(), "inputs": {}, "native": Mock(complete=False, lock=asyncio.Lock(), trace=None, display=None), "display": Mock(status=AsyncMock())}
    return instance


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_approval_has_safe_context_and_resumes_exact_waiter(self):
        instance = runtime()
        task = asyncio.create_task(instance.permission("Bash", {"command": "echo fixture"},
                                                       SimpleNamespace(tool_use_id="call", signal=None)))
        async with asyncio.timeout(1):
            while not instance.client.send_content.called:
                if task.done():
                    await task
                await asyncio.sleep(0)
        metadata = instance.client.send_content.call_args.args[1]["org.agentteams.approval"]
        self.assertEqual(metadata["reply_format"], "com.agentteams.answer")
        self.assertRegex(metadata["context_id"], r"^[\w.:-]+$")
        answer = {"kind": "tool_approval", "confirmation_id": metadata["request_id"],
                  "context_id": metadata["context_id"], "answer": "approved", "actor": "@human:test"}
        self.assertTrue(await instance.control(source("@worker 批准请求", **{"com.agentteams.answer": answer})))
        from qoder_agent_sdk import PermissionResultAllow
        self.assertIsInstance(await task, PermissionResultAllow)
        self.assertEqual(instance.client.send_content.call_args.args[1]["org.agentteams.approval"]["status"], "approved")

    async def test_native_permission_cancellation_reports_cancelled(self):
        instance = runtime()
        signal = asyncio.Event()
        task = asyncio.create_task(instance.permission("Bash", {}, SimpleNamespace(tool_use_id="call", signal=signal)))
        async with asyncio.timeout(1):
            while not instance.pending_requests:
                if task.done():
                    await task
                await asyncio.sleep(0)
        signal.set()
        from qoder_agent_sdk import PermissionResultDeny
        self.assertIsInstance(await asyncio.wait_for(task, 1), PermissionResultDeny)
        self.assertEqual(instance.client.send_content.call_args.args[1]["org.agentteams.approval"]["status"], "cancelled")

    async def test_public_stop_uses_native_interrupt_and_stopping_receipt(self):
        instance = runtime()
        self.assertTrue(await instance.control(source("停止当前执行", **{"com.agentteams.control": {
            "action": "stop", "agent_id": "@worker:test", "run_id": "$run"}})))
        instance.active["client"].interrupt.assert_awaited_once()
        instance.active["display"].status.assert_awaited_with("stopping")

    async def test_steer_is_not_retired_before_native_commit_and_result(self):
        instance = runtime()
        item = source("仅保留中文报告", **{"com.agentteams.control": {
            "action": "steer", "agent_id": "@worker:test", "run_id": "$run"}})
        self.assertTrue(await instance.control(item))
        call = instance.active["client"].query.await_args
        self.assertEqual(call.kwargs["priority"], "next")
        import uuid
        self.assertEqual(call.kwargs["message_uuid"], str(uuid.uuid5(uuid.NAMESPACE_URL, "$control")))
        instance.matrix.message_finish_tools.assert_not_called()
        instance.matrix.message_cancel.assert_not_called()
        self.assertTrue(await instance.control(item))
        instance.active["client"].query.assert_awaited_once()

    async def test_stale_stop_is_consumed_without_touching_current_run(self):
        instance = runtime()
        self.assertTrue(await instance.control(source("stop", **{"com.agentteams.control": {
            "action": "stop", "agent_id": "@worker:test", "run_id": "$old"}})))
        instance.active["client"].interrupt.assert_not_awaited()

    async def test_late_steer_remains_normal_input(self):
        instance = runtime()
        instance.active["accepting"] = False
        self.assertFalse(await instance.control(source("补充", **{"com.agentteams.control": {
            "action": "steer", "agent_id": "@worker:test", "run_id": "$run"}})))
        instance.active["client"].query.assert_not_awaited()


class TransportTests(unittest.TestCase):
    def test_saved_reply_binds_device_and_sends_through_real_http_transport(self):
        from worker_matrix_raw import publish_raw_saved
        received, bound = [], []
        class Server(BaseHTTPRequestHandler):
            def do_GET(self):
                encrypted = "/state/m.room.encryption/" in self.path
                self.send_response(404 if encrypted else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"errcode": "M_NOT_FOUND"} if encrypted else {
                    "user_id": "@worker:test", "device_id": "fixture-device"}).encode())
            def do_PUT(self):
                received.append((bool(bound), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"event_id":"$sent"}')
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = MatrixClient({"homeserver": f"http://127.0.0.1:{server.server_port}", "accessToken": "synthetic"})
            checkpoint = {"version": 1, "delivery": "pending", "reply_txn": "fixture-txn", "reply_attempt": {"state": "ready"},
                          "result": {"size": 6, "sha256": hashlib.sha256(b"result").hexdigest()}}
            event = publish_raw_saved(client, "!room", "result", checkpoint, "@worker:test", bound.append,
                content={"m.relates_to": {"m.in_reply_to": {"event_id": "$source"}}})
            self.assertEqual(event, "$sent")
            self.assertTrue(received[0][0])
            self.assertEqual(received[0][1]["m.relates_to"]["m.in_reply_to"]["event_id"], "$source")
            self.assertEqual(bound[0]["reply_attempt"]["device_id"], "fixture-device")
        finally:
            server.shutdown()
            server.server_close()

    def test_real_shared_delivery_uses_supported_client_method(self):
        from worker_matrix_raw import publish_raw_saved
        client = MatrixClient({})
        client.request = Mock(return_value={"event_id": "$sent"})
        async def deliver(_checkpoint, _text, _whoami, _persist, _find, send):
            return await send("fixture-txn")
        with patch("worker_matrix_raw.publish_saved", deliver), patch("worker_matrix_raw.require_unencrypted_room"):
            self.assertEqual(publish_raw_saved(client, "!room", "result", {}, "@worker:test", lambda _: None), "$sent")
        self.assertEqual(client.request.call_args.args[2]["org.agentteams.reply_txn"], "fixture-txn")
