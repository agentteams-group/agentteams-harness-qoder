"""Publish native facts through the shared Matrix runtime block protocol."""
import asyncio
import hashlib
import json
import time


class TurnDisplay:
    def __init__(self, client, source, *, direct=False, thinking=True, tools=True):
        self.client, self.source, self.direct = client, source, direct
        self.thinking, self.tools = thinking, tools
        self.events, self.calls, self.fragments = {}, {}, {}
        self.lock = asyncio.Lock()
        self.last_fragment = 0

    async def publish(self, key, blocks, body):
        async with self.lock:
            content = {"msgtype": "m.text", "body": body, "org.agentteams.progress_only": True,
                       "org.agentteams.run": {"version": "1", "run_id": self.source["event_id"], "blocks": blocks}}
            if self.source["event_id"].startswith("$"):
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": self.source["event_id"]}}
            digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            txn = "qoder:" + self.source["event_id"] + ":" + key + ":" + digest
            if key in self.events:
                content = {**content, "body": "* " + body, "m.new_content": content,
                           "m.relates_to": {"rel_type": "m.replace", "event_id": self.events[key]}}
            sent = await asyncio.to_thread(self.client.send_content, self.source["room_id"], content, txn)
            self.events.setdefault(key, sent)

    async def status(self, status, message_id=None):
        if not self.direct:
            return
        labels = {"running": "正在执行", "stopping": "正在停止", "stopped": "已停止",
                  "completed": "本轮已结束", "failed": "执行失败", "unavailable": "执行状态待核验"}
        await asyncio.to_thread(self.client.send_content, self.source["room_id"], {
            "msgtype": "m.notice", "body": labels[status], "org.agentteams.progress_only": True,
            "com.agentteams.execution": {"run_id": self.source["event_id"], "status": status,
                "capabilities": ["stop", "steer"], "steering_mode": "native",
                **({"message_id": message_id} if message_id else {})}},
            "qoder:" + self.source["event_id"] + ":execution:" + status)

    async def tool(self, name, identity, arguments=None, *, result=None, failed=False, finished=False):
        if not self.tools or not identity:
            return
        block = self.calls.setdefault(identity, {"type": "tool_call", "tool_name": name,
            "tool_call_id": identity, "arguments": arguments or {}, "started_at": int(time.time() * 1000)})
        terminal = finished or result is not None or failed
        block["status"] = "failed" if failed else "succeeded" if terminal else "running"
        if terminal:
            block["finished_at"] = int(time.time() * 1000)
            block["result"] = str(result)[:8192]
        await self.publish("tool:" + identity, [dict(block)], name + ": " + block["status"])

    async def message(self, message):
        from qoder_agent_sdk import AssistantMessage, ThinkingBlock, StreamEvent

        if not self.thinking:
            return
        if isinstance(message, AssistantMessage):
            texts = [block.thinking for block in message.content if isinstance(block, ThinkingBlock)]
            if not texts and self.fragments.get("text"):
                texts = [self.fragments["text"]]
            for index, text in enumerate(texts):
                identity = self.fragments.get("id") if index == 0 else None
                identity = identity or (message.message_id or message.uuid or "native") + ":" + str(index)
                await self.publish("thinking:" + identity,
                    [{"type": "thinking", "content": text[:16000], "isStreaming": False}], "思考过程")
            self.fragments.clear()
        elif isinstance(message, StreamEvent):
            event = message.event
            if event.get("type") == "message_start":
                self.fragments.clear()
                self.fragments["id"] = (event.get("message") or {}).get("id") or message.uuid
            delta = event.get("delta") or {}
            if delta.get("type") == "thinking_delta":
                self.fragments["text"] = (self.fragments.get("text", "") + delta.get("thinking", ""))[-16000:]
                if time.monotonic() - self.last_fragment >= 1:
                    self.last_fragment = time.monotonic()
                    await self.publish("thinking:" + self.fragments.get("id", message.uuid),
                        [{"type": "thinking", "content": self.fragments["text"], "isStreaming": True}], "正在思考")

    async def close(self, *, failed=False):
        if self.fragments.get("text"):
            await self.publish("thinking:" + self.fragments.get("id", "last"),
                [{"type": "thinking", "content": self.fragments["text"], "isStreaming": False}], "思考过程")
        if failed:
            await self.publish("error", [{"type": "error", "kind": "failed", "title": "本轮执行未完成"}], "本轮执行未完成")
