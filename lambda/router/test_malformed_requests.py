"""Malformed webhook requests get a 4xx response instead of raising (release/v1).

Port of main's test_malformed_requests.py (#215) to the channels release/v1
has (Telegram, Slack, Feishu), plus the v1 gaps: Slack and Feishu requests
whose body was parsed before the signature check, Feishu timestamp replay,
and timestamps too large to compare. All secrets and IDs below are synthetic
placeholders.
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

WEBHOOK_SECRET = "synthetic-telegram-webhook-secret"
SLACK_SIGNING = "synthetic-slack-signing-secret"
FEISHU_TOKEN = "synthetic-feishu-verification-token"
FEISHU_KEY = "synthetic-feishu-encrypt-key"

INVALID_B64 = "!!!not base64!!!"
INVALID_UTF8_B64 = base64.b64encode(b"\xff\xfe\x80").decode("ascii")
NON_DICT_BODIES = ("[]", "[1, 2]", '"text"', "42", "null", "true")
DEEPLY_NESTED = "[" * 100000 + "]" * 100000
HUGE_TIMESTAMP = "9" * 400


def _event(channel, body="{}", headers=None, b64=False):
    path = f"/webhook/{channel}"
    return {"requestContext": {"http": {"method": "POST", "path": path, "sourceIp": "192.0.2.1"}},
            "rawPath": path, "headers": headers, "body": body, "isBase64Encoded": b64}


# --- valid-credential header builders per channel ----------------------------

def _telegram_headers(body):
    return {"x-telegram-bot-api-secret-token": WEBHOOK_SECRET}


def _slack_headers(body, ts=None):
    ts = ts or str(int(time.time()))
    sig = "v0=" + hmac.new(SLACK_SIGNING.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _feishu_headers(body, ts=None):
    ts, nonce = ts or str(int(time.time())), "synthetic-nonce"
    sig = hashlib.sha256(f"{ts}{nonce}{FEISHU_KEY}".encode() + body.encode()).hexdigest()
    return {"x-lark-request-timestamp": ts, "x-lark-request-nonce": nonce, "x-lark-signature": sig}


SIGNED = {
    "telegram": _telegram_headers,
    "slack": _slack_headers,
    "feishu": _feishu_headers,
}

ALL_CHANNELS = tuple(SIGNED)


# Signature header(s) per channel with a non-ASCII value; other headers valid.
def _non_ascii_headers(channel):
    if channel == "telegram":
        return {"x-telegram-bot-api-secret-token": "\u00e9t\u00e9"}
    if channel == "slack":
        return {"x-slack-request-timestamp": str(int(time.time())), "x-slack-signature": "v0=\u00e9"}
    if channel == "feishu":
        return {"x-lark-request-timestamp": str(int(time.time())), "x-lark-request-nonce": "n",
                "x-lark-signature": "\u00e9" * 64}
    raise ValueError(channel)


def _challenge(**extra):
    return json.dumps({"type": "url_verification", "challenge": "abc123", **extra})


class _ChannelsConfigured(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(index, "_get_webhook_secret", return_value=WEBHOOK_SECRET),
            patch.object(index, "_get_slack_tokens", return_value=("xoxb-synthetic", SLACK_SIGNING)),
            patch.object(index, "_get_feishu_credentials",
                         return_value=("app", "secret", FEISHU_TOKEN, FEISHU_KEY)),
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
        return index._handle_http_request(event)


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

    def test_huge_timestamp_is_401(self):
        """A timestamp too large to subtract from a float is rejected, not raised."""
        body = "{}"
        for ch, headers in (("slack", _slack_headers(body, ts=HUGE_TIMESTAMP)),
                            ("feishu", _feishu_headers(body, ts=HUGE_TIMESTAMP)),
                            ("feishu", _feishu_headers(body, ts="-" + HUGE_TIMESTAMP))):
            with self.subTest(channel=ch, ts=headers.get("x-slack-request-timestamp",
                                                         headers.get("x-lark-request-timestamp"))[:4]):
                self.assertEqual(self.call(_event(ch, body, headers))["statusCode"], 401)
        self.mock_async.assert_not_called()


class TestReplay(_ChannelsConfigured):
    def test_signed_stale_request_is_401(self):
        """A correctly signed request is not accepted outside the 5-minute window."""
        body = json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}})
        for ch, sign in (("slack", _slack_headers), ("feishu", _feishu_headers)):
            for ts in ("1000000000", str(int(time.time()) - 600), str(int(time.time()) + 600)):
                with self.subTest(channel=ch, ts=ts):
                    self.assertEqual(self.call(_event(ch, body, sign(body, ts=ts)))["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_signed_fresh_request_is_dispatched(self):
        body = json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}})
        for ch, sign in (("slack", _slack_headers), ("feishu", _feishu_headers)):
            for age in (0, 240):
                with self.subTest(channel=ch, age=age):
                    self.mock_async.reset_mock()
                    headers = sign(body, ts=str(int(time.time()) - age))
                    self.assertEqual(self.call(_event(ch, body, headers)), {"statusCode": 200, "body": "ok"})
                    self.mock_async.assert_called_once_with(ch, body, headers)


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

    def test_unsigned_non_dict_json_is_401(self):
        """Slack and Feishu used to read these bodies before the signature check."""
        for ch in ALL_CHANNELS:
            for body in NON_DICT_BODIES + (DEEPLY_NESTED,):
                with self.subTest(channel=ch, body=body[:8]):
                    resp = self.call(_event(ch, body, {}))
                    self.assertEqual(resp["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_signed_non_dict_json_is_400(self):
        for ch, sign in SIGNED.items():
            for body in NON_DICT_BODIES + (DEEPLY_NESTED,):
                with self.subTest(channel=ch, body=body[:8]):
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


class TestUrlVerification(_ChannelsConfigured):
    def test_unsigned_slack_url_verification_is_401(self):
        for headers in ({}, {**_slack_headers(_challenge()), "x-slack-signature": "v0=" + "0" * 64}):
            with self.subTest(headers=sorted(headers)):
                resp = self.call(_event("slack", _challenge(), headers))
                self.assertEqual(resp["statusCode"], 401)
                self.assertNotIn("abc123", resp["body"])

    def test_slack_url_verification_still_answered(self):
        body = _challenge()
        resp = self.call(_event("slack", body, _slack_headers(body)))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"]), {"challenge": "abc123"})
        self.mock_async.assert_not_called()

    def test_feishu_url_verification_without_its_token_is_401(self):
        """Signature headers do not stand in for the Verification Token."""
        for body in (_challenge(), _challenge(token="wrong"), _challenge(token=None),
                     _challenge(token=["x"]), _challenge(token="\u00e9")):
            for headers in ({}, None):
                with self.subTest(body=body, headers=headers):
                    resp = self.call(_event("feishu", body, headers))
                    self.assertEqual(resp["statusCode"], 401)
                    self.assertNotIn("abc123", resp["body"])
        body = _challenge()
        self.assertEqual(self.call(_event("feishu", body, _feishu_headers(body)))["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_feishu_url_verification_before_credentials_are_stored(self):
        """No Feishu credentials stored yet (first-time setup): the challenge is
        answered as before, and signed requests still fail closed."""
        with patch.object(index, "_get_feishu_credentials", return_value=("", "", "", "")):
            resp = self.call(_event("feishu", _challenge(token="any"), {}))
            self.assertEqual(resp["statusCode"], 200)
            self.assertEqual(json.loads(resp["body"]), {"challenge": "abc123"})
            body = json.dumps({"schema": "2.0"})
            self.assertEqual(self.call(_event("feishu", body, _feishu_headers(body)))["statusCode"], 401)
        self.mock_async.assert_not_called()

    def test_feishu_url_verification_still_answered(self):
        resp = self.call(_event("feishu", _challenge(token=FEISHU_TOKEN), {}))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"]), {"challenge": "abc123"})
        self.mock_async.assert_not_called()

    def test_feishu_url_verification_challenge_format_still_checked(self):
        body = json.dumps({"type": "url_verification", "challenge": "<b>x</b>", "token": FEISHU_TOKEN})
        self.assertEqual(self.call(_event("feishu", body, {}))["statusCode"], 400)


class TestValidRequestsUnchanged(_ChannelsConfigured):
    def test_signed_object_is_dispatched(self):
        body = json.dumps({"update_id": 1, "type": "event_callback",
                           "schema": "2.0", "header": {"event_type": "im.message.receive_v1"}})
        for ch, sign in SIGNED.items():
            with self.subTest(channel=ch):
                self.mock_async.reset_mock()
                headers = sign(body)
                resp = self.call(_event(ch, body, headers))
                self.assertEqual(resp, {"statusCode": 200, "body": "ok"})
                self.mock_async.assert_called_once_with(ch, body, headers)

    def test_signed_base64_object_is_dispatched(self):
        body = json.dumps({"update_id": 1})
        encoded = base64.b64encode(body.encode()).decode()
        resp = self.call(_event("telegram", encoded, _telegram_headers(body), b64=True))
        self.assertEqual(resp, {"statusCode": 200, "body": "ok"})
        self.mock_async.assert_called_once_with("telegram", body, _telegram_headers(body))

    def test_signed_feishu_encrypted_envelope_is_dispatched(self):
        body = json.dumps({"encrypt": "c3ludGhldGlj"})
        headers = _feishu_headers(body)
        self.assertEqual(self.call(_event("feishu", body, headers)), {"statusCode": 200, "body": "ok"})
        self.mock_async.assert_called_once_with("feishu", body, headers)

    def test_slack_retry_still_acknowledged_without_dispatch(self):
        body = json.dumps({"type": "event_callback"})
        headers = {**_slack_headers(body), "x-slack-retry-num": "1"}
        self.assertEqual(self.call(_event("slack", body, headers)), {"statusCode": 200, "body": "ok"})
        self.mock_async.assert_not_called()

    def test_wrong_signature_is_still_401(self):
        body = "{}"
        bad = {
            "telegram": {"x-telegram-bot-api-secret-token": "wrong"},
            "slack": {**_slack_headers(body), "x-slack-signature": "v0=" + "0" * 64},
            "feishu": {**_feishu_headers(body), "x-lark-signature": "0" * 64},
        }
        for ch, headers in bad.items():
            with self.subTest(channel=ch):
                self.assertEqual(self.call(_event(ch, body, headers))["statusCode"], 401)
        self.mock_async.assert_not_called()


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

    def test_async_dispatch_failure_is_not_swallowed(self):
        """The internal async path stays unguarded: a failure keeps its
        traceback and Lambda's async retries, as before."""
        with patch.object(index, "handle_telegram", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                index.handler({"_async_dispatch": True, "_channel": "telegram", "_body": "{}"}, None)


class TestSafeCompare(unittest.TestCase):
    def test_values(self):
        self.assertTrue(index._safe_compare("abc", "abc"))
        self.assertFalse(index._safe_compare("abc", "abd"))
        self.assertFalse(index._safe_compare("abc", "\u00e9"))
        self.assertFalse(index._safe_compare("abc", None))
        self.assertFalse(index._safe_compare(None, "abc"))
        self.assertFalse(index._safe_compare("abc", "\ud800"))
        self.assertTrue(index._safe_compare("\u00e9", "\u00e9"))


if __name__ == "__main__":
    unittest.main()
