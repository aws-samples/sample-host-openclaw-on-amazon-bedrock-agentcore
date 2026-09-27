"""Tests for Feishu delivery in the Cron Lambda.

A schedule created by a Feishu user carries channel="feishu". The cron
Lambda must route that result to send_feishu_message, which authenticates
with a tenant_access_token obtained from the app credentials in the Feishu
secret (same flow as the router Lambda).
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("FEISHU_TOKEN_SECRET_ID", "openclaw/channels/feishu")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

index = importlib.import_module("index")

FEISHU_SECRET = json.dumps({
    "appId": "cli_xxxxxxxx",
    "appSecret": "test-app-secret",
    "verificationToken": "test-verification-token",
    "encryptKey": "",
})


def _response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    return resp


class FeishuTestBase(unittest.TestCase):
    def setUp(self):
        index._token_cache.clear()
        cache = getattr(index, "_feishu_token_cache", None)
        if cache is not None:
            cache.update({"token": "", "expires_at": 0})
        self.secrets = MagicMock()
        self.secrets.get_secret_value.return_value = {"SecretString": FEISHU_SECRET}
        p = patch.object(index, "secrets_client", self.secrets)
        p.start()
        self.addCleanup(p.stop)
        # Point the module at the secret id regardless of import order.
        p2 = patch.object(index, "FEISHU_TOKEN_SECRET_ID", "openclaw/channels/feishu", create=True)
        p2.start()
        self.addCleanup(p2.stop)

    def _fake_urlopen(self):
        """urlopen that answers the token endpoint and records message posts."""
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            if "tenant_access_token" in req.full_url:
                return _response({"code": 0, "tenant_access_token": "t-test-token", "expire": 7200})
            return _response({"code": 0, "msg": "success"})

        return fake, calls


class TestDeliverResponseFeishu(FeishuTestBase):
    def test_feishu_channel_reaches_feishu_sender(self):
        with patch.object(index, "send_feishu_message") as sender:
            index.deliver_response("feishu", "ou_xxxxxxxx", "Your morning brief")
        sender.assert_called_once_with("ou_xxxxxxxx", "Your morning brief")

    def test_feishu_content_blocks_are_flattened(self):
        blocks = json.dumps([{"type": "text", "text": "Hello from cron"}])
        with patch.object(index, "send_feishu_message") as sender:
            index.deliver_response("feishu", "ou_xxxxxxxx", blocks)
        sender.assert_called_once()
        self.assertEqual(sender.call_args[0][1], "Hello from cron")


class TestSendFeishuMessage(FeishuTestBase):
    def test_posts_text_message_with_bearer_token(self):
        fake, calls = self._fake_urlopen()
        with patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.send_feishu_message("ou_xxxxxxxx", "Your morning brief")

        self.secrets.get_secret_value.assert_called_with(SecretId="openclaw/channels/feishu")
        self.assertEqual(len(calls), 2)
        token_req, msg_req = calls

        self.assertEqual(
            token_req.full_url,
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        )
        self.assertEqual(
            json.loads(token_req.data),
            {"app_id": "cli_xxxxxxxx", "app_secret": "test-app-secret"},
        )

        self.assertEqual(
            msg_req.full_url,
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        )
        self.assertEqual(msg_req.get_method(), "POST")
        self.assertEqual(msg_req.get_header("Authorization"), "Bearer t-test-token")
        body = json.loads(msg_req.data)
        self.assertEqual(body["receive_id"], "ou_xxxxxxxx")
        self.assertEqual(body["msg_type"], "text")
        self.assertEqual(json.loads(body["content"]), {"text": "Your morning brief"})

    def test_chat_id_target_uses_chat_id_receive_type(self):
        fake, calls = self._fake_urlopen()
        with patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.send_feishu_message("oc_xxxxxxxx", "Group brief")
        self.assertTrue(calls[-1].full_url.endswith("receive_id_type=chat_id"))

    def test_tenant_token_is_cached_between_sends(self):
        fake, calls = self._fake_urlopen()
        with patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.send_feishu_message("ou_xxxxxxxx", "one")
            index.send_feishu_message("ou_xxxxxxxx", "two")
        token_calls = [c for c in calls if "tenant_access_token" in c.full_url]
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(calls), 3)

    def test_missing_credentials_sends_nothing(self):
        self.secrets.get_secret_value.return_value = {"SecretString": "{}"}
        fake, calls = self._fake_urlopen()
        with patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.send_feishu_message("ou_xxxxxxxx", "text")
        self.assertEqual(calls, [])

    def test_token_error_sends_nothing(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            return _response({"code": 10003, "msg": "invalid app"})

        with patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.send_feishu_message("ou_xxxxxxxx", "text")
        self.assertEqual(len(calls), 1)
        self.assertIn("tenant_access_token", calls[0].full_url)


if __name__ == "__main__":
    unittest.main()
