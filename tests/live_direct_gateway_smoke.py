"""Opt-in direct-provider probe against a local OpenAI-compatible fixture."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading

from qoder_agent_sdk import QoderAgentOptions, ResultMessage, qodercli_auth, query


async def run(enable_sdk_direct):
    requests = []
    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "model": body.get("model"),
                             "auth_matches": self.headers.get("Authorization") == "Bearer fixture-not-secret"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": "DIRECT_GATEWAY_OK"}, None), ({}, "stop")]:
                chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                         "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix="qoder-direct-gateway-") as cwd:
            settings = {"providers": {"agentteams-gateway": {
                "type": "openai-compatible", "protocol": "openai",
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                "apiKey": "fixture-not-secret", "model": "fixture"}}}
            options = QoderAgentOptions(auth=qodercli_auth(), cwd=cwd, tools=[], setting_sources=[],
                settings=settings, model="agentteams-gateway/fixture", max_turns=1,
                env={"QODER_SDK_CUSTOM_BASE_URL_BYOK": "1"} if enable_sdk_direct else {})
            async with asyncio.timeout(60):
                async for message in query(prompt="Return the gateway response.", options=options):
                    if isinstance(message, ResultMessage):
                        result = {"result": message.result, "errors": message.errors, "subtype": message.subtype}
            print(json.dumps({"sdk_direct_enabled": enable_sdk_direct, "outcome": result,
                              "gateway_requests": requests}, ensure_ascii=False))
            return bool(result and result["result"] == "DIRECT_GATEWAY_OK" and requests
                        and all(row["auth_matches"] for row in requests))
    finally:
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-sdk-direct", action="store_true",
                        help="Enable the pinned CLI's SDK custom-base-URL feature; account checks still apply")
    raise SystemExit(0 if asyncio.run(run(parser.parse_args().enable_sdk_direct)) else 1)
