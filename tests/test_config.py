import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from agentteams_qoder.config import authentication, gateway_options, load
from agentteams_qoder.transport import allowed


def record():
    return {"apiVersion": "agentteams.io/v1beta1", "kind": "MemberRuntimeConfig",
            "member": {"runtime": "qoder", "runtimeName": "worker", "role": "worker", "matrixUserId": "@worker:test", "personalRoomId": "!dm:test"},
            "storage": {"memberPrefix": "agents/worker"},
            "matrix": {"transport": {"homeserver": "http://matrix", "encryption": False,
                "dm": {"policy": "allowlist", "allowFrom": ["@human:test"]}, "groupPolicy": "allowlist", "groupAllowFrom": ["@leader:test"]}},
            "desired": {"model": {"model": "test-model", "gatewayUrl": "http://gateway"}}}


class ConfigTests(unittest.TestCase):
    def test_direct_gateway_uses_only_projected_configuration(self):
        model = {"model": "gateway-model", "url": "https://gateway/v1", "api_key": "fixture-gateway"}
        for env in ({}, {"QODER_BYOK_PROVIDER": "old-provider", "QODER_MODEL": "auto",
                         "QODER_MODEL_SOURCE": "service"}):
            with patch.dict(os.environ, env, clear=True):
                options = gateway_options(model)
            self.assertEqual(options["model"], "agentteams-gateway/gateway-model")
            self.assertEqual(options["settings"], {"providers": {"agentteams-gateway": {
                "type": "openai-compatible", "protocol": "openai", "model": "gateway-model",
                "baseUrl": "https://gateway/v1", "apiKey": "fixture-gateway"}}})

    def test_incomplete_gateway_fails_instead_of_selecting_account_model(self):
        model = {"model": "gateway-model", "url": "https://gateway/v1", "api_key": "fixture-gateway"}
        for field in model:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "gateway"):
                gateway_options({**model, field: ""})

    def test_projected_gateway_and_role(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "AGENTTEAMS_WORKER_NAME": "worker", "AGENTTEAMS_WORKER_GATEWAY_KEY": "fixture-gateway", "AGENTTEAMS_WORKER_MATRIX_TOKEN": "fixture-matrix"}):
            path = Path(directory) / "runtime.yaml"
            source = record()
            source["member"]["role"] = "team_leader"
            path.write_text(yaml.safe_dump(source))
            _, role, matrix, model = load(path)
            self.assertEqual(role, "leader")
            self.assertEqual(matrix["accessToken"], "fixture-matrix")
            self.assertEqual(model["url"], "http://gateway/v1")
            self.assertNotIn("fixture-gateway", path.read_text())

    def test_unconfigured_qoder_auth_fails_explicitly(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "QODER"):
                authentication()

    def test_direct_sender_policy_and_self_echo(self):
        config = record()
        event = {"sender": "@human:test", "content": {"msgtype": "m.text", "body": "hello"}}
        self.assertTrue(allowed(config, "!dm:test", event))
        self.assertFalse(allowed(config, "!dm:test", {**event, "sender": "@attacker:test"}))
        self.assertFalse(allowed(config, "!dm:test", {**event, "sender": "@worker:test"}))

    def test_operator_control_does_not_bypass_explicit_channel_denial(self):
        config = record()
        config["team"] = {"teamRoomId": "!team:test", "admin": {"matrixUserId": "@human:test"}}
        event = {"sender": "@human:test", "content": {"msgtype": "m.text", "body": "批准"}}
        self.assertTrue(allowed(config, "!team:test", event, control=True))
        self.assertFalse(allowed(config, "!team:test", event))
        config["desired"]["channelPolicy"] = {"groupDenyExtra": ["@human:test"]}
        self.assertFalse(allowed(config, "!team:test", event, control=True))

    def test_general_team_room_requires_structured_mention(self):
        config = record()
        config["team"] = {"teamRoomId": "!team:test"}
        event = {"sender": "@leader:test", "content": {"msgtype": "m.text", "body": "hello"}}
        self.assertFalse(allowed(config, "!team:test", event))
        event["content"]["m.mentions"] = {"user_ids": ["@worker:test"]}
        self.assertTrue(allowed(config, "!team:test", event))


if __name__ == "__main__":
    unittest.main()
