"""Opt-in direct-provider probe against a local OpenAI-compatible fixture."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading

from qoder_agent_sdk import QoderAgentOptions, QoderSDKClient, ResultMessage, qodercli_auth

from agentteams_qoder.config import gateway_options


async def run():
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
    selected_model_enabled = False
    try:
        with tempfile.TemporaryDirectory(prefix="qoder-direct-gateway-") as cwd:
            gateway = gateway_options({"url": f"http://127.0.0.1:{server.server_port}/v1",
                                       "api_key": "fixture-not-secret", "model": "fixture"})
            async def resolve_model(_context):
                return {"model": gateway["model"]}
            options = QoderAgentOptions(auth=qodercli_auth(), cwd=cwd, tools=[], setting_sources=[],
                **gateway, resolve_model=resolve_model, max_turns=1)
            async with asyncio.timeout(60):
                async with QoderSDKClient(options=options) as client:
                    models = await client.get_available_models()
                    selected_model_enabled = any(model.get("value") == gateway["model"] and model.get("isEnabled")
                                                 for model in models)
                    await client.query("Return the gateway response.")
                    async for message in client.receive_response():
                        if isinstance(message, ResultMessage):
                            result = {"result": message.result, "errors": message.errors, "subtype": message.subtype}
            print(json.dumps({"route": "openai-compatible direct", "outcome": result,
                              "selected_model_enabled": selected_model_enabled,
                              "gateway_requests": requests}, ensure_ascii=False))
            return bool(result and result["result"] == "DIRECT_GATEWAY_OK" and requests
                        and all(row["auth_matches"] and row["model"] == "fixture"
                                and row["path"] == "/v1/chat/completions" for row in requests))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    raise SystemExit(0 if asyncio.run(run()) else 1)
