"""Malformed webhook requests get a 4xx response instead of raising.

Covers each channel's HTTP entry point with non-ASCII and missing signature
headers, undecodable base64 / UTF-8 bodies, empty bodies and JSON bodies that
are not objects. All secrets and IDs below are synthetic placeholders.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")
line_channel = importlib.import_module("line_channel")
whatsapp = importlib.import_module("whatsapp")

WEBHOOK_SECRET = "synthetic-telegram-webhook-secret"
SLACK_SIGNING = "synthetic-slack-signing-secret"
FEISHU_KEY = "synthetic-feishu-encrypt-key"
WA_CONFIG = {"accessToken": "synthetic-wa-token", "appSecret": "synthetic-wa-app-secret",
             "verifyToken": "synthetic-verify", "phoneNumberId": "100000000000001",
             "graphApiVersion": "v21.0"}
LINE_CONFIG = {"channelSecret": "0123456789abcdef0123456789abcdef",
               "channelAccessToken": "synthetic-line-token", "botUserId": ""}
DISCORD_CONFIG = {"applicationId": "400000000000000001", "publicKey": "ab" * 32,
                  "botToken": "synthetic.bot.token"}

NON_ASCII = "sha256=\u00e9\u00e9\u00e9"
INVALID_B64 = "!!!not base64!!!"
INVALID_UTF8_B64 = base64.b64encode(b"\xff\xfe\x80").decode("ascii")
NON_DICT_BODIES = ("[]", "[1, 2]", '"text"', "42", "null", "true")


def _event(channel, body="{}", headers=None, b64=False):
    path = f"/webhook/{channel}"
    return {"requestContext": {"http": {"method": "POST", "path": path, "sourceIp": "192.0.2.1"}},
            "rawPath": path, "headers": headers, "body": body, "isBase64Encoded": b64}


# --- valid-signature header builders per channel -----------------------------

def _telegram_headers(body):
    return {"x-telegram-bot-api-secret-token": WEBHOOK_SECRET}


def _slack_headers(body):
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SLACK_SIGNING.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _feishu_headers(body):
    ts, nonce = str(int(time.time())), "synthetic-nonce"
    sig = hashlib.sha256(f"{ts}{nonce}{FEISHU_KEY}".encode() + body.encode()).hexdigest()
    return {"x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce, "x-lark-signature": sig}


def _whatsapp_headers(body):
    sig = hmac.new(WA_CONFIG["appSecret"].encode(), body.encode(), hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": "sha256=" + sig}


def _line_headers(body):
    sig = base64.b64encode(hmac.new(LINE_CONFIG["channelSecret"].encode(), body.encode(),
                                    hashlib.sha256).digest()).decode()
    return {"x-line-signature": sig}


SIGNED = {
    "telegram": _telegram_headers,
    "slack": _slack_headers,
    "feishu": _feishu_headers,
    "whatsapp": _whatsapp_headers,
    "line": _line_headers,
}

# Signature header(s) per channel with a non-ASCII value; other headers valid.
def _non_ascii_headers(channel):
    if channel == "telegram":
        return {"x-telegram-bot-api-secret-token": "\u00e9t\u00e9"}
    if channel == "slack":
        return {"x-slack-request-timestamp": str(int(time.time())), "x-slack-signature": "v0=\u00e9"}
    if channel == "feishu":
        return {"x-lark-request-timestamp": str(int(time.time())), "x-lark-request-nonce": "n",
                "x-lark-signature": "\u00e9" * 64}
    if channel == "whatsapp":
        return {"x-hub-signature-256": NON_ASCII}
    if channel == "line":
        return {"x-line-signature": "\u00e9" * 43 + "="}
    if channel == "discord":
        return {"x-signature-ed25519": "\u00e9" * 128, "x-signature-timestamp": str(int(time.time()))}
    raise ValueError(channel)


ALL_CHANNELS = ("telegram", "slack", "feishu", "whatsapp", "line", "discord")


class _ChannelsConfigured(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(index, "_get_webhook_secret", return_value=WEBHOOK_SECRET),
            patch.object(index, "_get_slack_tokens", return_value=("xoxb-synthetic", SLACK_SIGNING)),
            patch.object(index, "_get_feishu_credentials",
                         return_value=("app", "secret", "vtoken", FEISHU_KEY)),
            patch.object(index, "_get_whatsapp_config", return_value=WA_CONFIG),
            patch.object(index, "_get_line_config", return_value=LINE_CONFIG),
            patch.object(index, "_get_discord_config", return_value=DISCORD_CONFIG),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(index, "_self_invoke_async")
        self.mock_async = p.start()
        self.addCleanup(p.stop)

    def call(self, event):
        # Call the inner router directly so an exception would surface here
        # rather than being absorbed by the last-resort guard in handler().
        return index._handle_event(event, None)


class TestMalformedHeaders(_ChannelsConfigured):
    def test_non_ascii_signature_header_is_401(self):
        for ch in ALL_CHANNELS:
            with self.subTest(channel=ch):
                resp = self.call(_event(ch, "{}", _non_ascii_headers(ch)))
                self.assertEqual(resp["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_missing_headers_is_401(self):
        for ch in ALL_CHANNELS:
            for headers in (None, {}):
                with self.subTest(channel=ch, headers=headers):
                    resp = self.call(_event(ch, "{}", headers))
                    self.assertEqual(resp["statusCode"], 401)
        self.mock_async.assert_not_called()


class TestMalformedBodies(_ChannelsConfigured):
    def test_invalid_base64_is_400(self):
        for ch in ALL_CHANNELS:
            with self.subTest(channel=ch):
                resp = self.call(_event(ch, INVALID_B64, {}, b64=True))
                self.assertEqual(resp["statusCode"], 400)
        self.mock_async.assert_not_called()

    def test_invalid_utf8_is_400(self):
        for ch in ALL_CHANNELS:
            with self.subTest(channel=ch):
                resp = self.call(_event(ch, INVALID_UTF8_B64, {}, b64=True))
                self.assertEqual(resp["statusCode"], 400)
        self.mock_async.assert_not_called()

    def test_non_string_base64_body_is_400(self):
        resp = self.call(_event("telegram", 12345, {}, b64=True))
        self.assertEqual(resp["statusCode"], 400)

    def test_signed_non_dict_json_is_400(self):
        for ch, sign in SIGNED.items():
            for body in NON_DICT_BODIES:
                with self.subTest(channel=ch, body=body):
                    resp = self.call(_event(ch, body, sign(body)))
                    self.assertEqual(resp["statusCode"], 400)
        self.mock_async.assert_not_called()

    def test_signed_non_json_is_400(self):
        for ch, sign in SIGNED.items():
            with self.subTest(channel=ch):
                body = "not json"
                resp = self.call(_event(ch, body, sign(body)))
                self.assertEqual(resp["statusCode"], 400)
        self.mock_async.assert_not_called()

    def test_signed_empty_body_is_400(self):
        for ch, sign in SIGNED.items():
            for body in ("", None):
                with self.subTest(channel=ch, body=body):
                    resp = self.call(_event(ch, body, sign("")))
                    self.assertEqual(resp["statusCode"], 400)
        self.mock_async.assert_not_called()

    def test_unsigned_empty_body_is_401(self):
        for ch in ALL_CHANNELS:
            for body in ("", None):
                with self.subTest(channel=ch, body=body):
                    resp = self.call(_event(ch, body, {}))
                    self.assertEqual(resp["statusCode"], 401)
        self.mock_async.assert_not_called()


class TestValidRequestsUnchanged(_ChannelsConfigured):
    def test_signed_object_is_dispatched(self):
        body = json.dumps({"update_id": 1, "events": [], "entry": [], "type": "event_callback"})
        for ch, sign in SIGNED.items():
            with self.subTest(channel=ch):
                self.mock_async.reset_mock()
                resp = self.call(_event(ch, body, sign(body)))
                self.assertEqual(resp, {"statusCode": 200, "body": "ok"})
                self.mock_async.assert_called_once()

    def test_signed_base64_object_is_dispatched(self):
        body = json.dumps({"update_id": 1})
        encoded = base64.b64encode(body.encode()).decode()
        resp = self.call(_event("telegram", encoded, _telegram_headers(body), b64=True))
        self.assertEqual(resp, {"statusCode": 200, "body": "ok"})
        self.mock_async.assert_called_once_with("telegram", body, _telegram_headers(body))

    def test_wrong_signature_is_still_401(self):
        body = "{}"
        bad = {
            "telegram": {"x-telegram-bot-api-secret-token": "wrong"},
            "slack": {**_slack_headers(body), "x-slack-signature": "v0=" + "0" * 64},
            "feishu": {**_feishu_headers(body), "x-lark-signature": "0" * 64},
            "whatsapp": {"x-hub-signature-256": "sha256=" + "0" * 64},
            "line": {"x-line-signature": "A" * 43 + "="},
        }
        for ch, headers in bad.items():
            with self.subTest(channel=ch):
                self.assertEqual(self.call(_event(ch, body, headers))["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_slack_url_verification_still_answered(self):
        body = json.dumps({"type": "url_verification", "challenge": "abc123"})
        resp = self.call(_event("slack", body, _slack_headers(body)))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"]), {"challenge": "abc123"})

    def test_feishu_url_verification_still_answered(self):
        body = json.dumps({"type": "url_verification", "challenge": "abc123"})
        resp = self.call(_event("feishu", body, _feishu_headers(body)))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"]), {"challenge": "abc123"})


class TestHandlerGuard(unittest.TestCase):
    def test_non_dict_event_is_400(self):
        for event in (None, [], "x"):
            with self.subTest(event=event):
                self.assertEqual(index.handler(event, None)["statusCode"], 400)

    def test_missing_request_context(self):
        resp = index.handler({"requestContext": None, "headers": None, "body": None}, None)
        self.assertEqual(resp["statusCode"], 405)

    def test_unexpected_error_returns_500_without_logging_body(self):
        body = '{"secret-marker": "do-not-log"}'
        with patch.object(index, "validate_telegram_webhook", side_effect=RuntimeError("do-not-log")), \
                self.assertLogs(index.logger, level="ERROR") as logs:
            resp = index.handler(_event("telegram", body, {}), None)
        self.assertEqual(resp, {"statusCode": 500, "body": "Internal Server Error"})
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("do-not-log", joined)


class TestSafeCompare(unittest.TestCase):
    def test_values(self):
        self.assertTrue(index._safe_compare("abc", "abc"))
        self.assertFalse(index._safe_compare("abc", "abd"))
        self.assertFalse(index._safe_compare("abc", "\u00e9"))
        self.assertFalse(index._safe_compare("abc", None))
        self.assertFalse(index._safe_compare(None, "abc"))
        self.assertFalse(index._safe_compare("abc", "\ud800"))
        self.assertTrue(index._safe_compare("\u00e9", "\u00e9"))


class TestSharedVerifySignature(unittest.TestCase):
    def test_whatsapp(self):
        body = b"{}"
        good = "sha256=" + hmac.new(b"app", body, hashlib.sha256).hexdigest()
        self.assertTrue(whatsapp.verify_signature({"x-hub-signature-256": good}, body, "app"))
        for sig in (NON_ASCII, "sha256=\ud800", None, 123, ""):
            with self.subTest(sig=sig):
                self.assertFalse(whatsapp.verify_signature({"x-hub-signature-256": sig}, body, "app"))
        self.assertFalse(whatsapp.verify_signature(None, body, "app"))

    def test_line(self):
        body = b"{}"
        good = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertTrue(line_channel.verify_signature({"x-line-signature": good}, body, "secret"))
        for sig in ("\u00e9" * 43 + "=", good + "\n", None, 123, ""):
            with self.subTest(sig=sig):
                self.assertFalse(line_channel.verify_signature({"x-line-signature": sig}, body, "secret"))
        self.assertFalse(line_channel.verify_signature(None, body, "secret"))


if __name__ == "__main__":
    unittest.main()
