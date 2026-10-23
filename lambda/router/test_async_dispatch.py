"""Tests for the signed async self-invocation (re-entry) path of the Router Lambda.

The router ACKs each webhook after its channel's signature check and re-invokes
itself asynchronously. The re-entry event must carry a valid authenticator, or
it is not dispatched to any channel handler.

All IDs, tokens and secrets below are synthetic placeholders.
"""

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

WEBHOOK_SECRET = "test-webhook-secret"
CHANNEL_HANDLERS = {
    "telegram": ("handle_telegram", False),
    "slack": ("handle_slack", True),
    "feishu": ("handle_feishu", True),
    "whatsapp": ("handle_whatsapp", False),
    "discord": ("handle_discord", False),
    "line": ("handle_line", False),
}


def _event(channel, body="{}", headers=None, auth=None):
    event = {"_async_dispatch": True, "_channel": channel, "_body": body, "_headers": headers or {}}
    if auth is not None:
        event["_auth"] = auth
    return event


class _Base(unittest.TestCase):
    def setUp(self):
        index._token_cache.clear()
        patcher = patch.object(index, "_get_webhook_secret", return_value=WEBHOOK_SECRET)
        self.secret = patcher.start()
        self.addCleanup(patcher.stop)
        self.handlers = {}
        for channel, (name, _) in CHANNEL_HANDLERS.items():
            p = patch.object(index, name)
            self.handlers[channel] = p.start()
            self.addCleanup(p.stop)

    def assert_nothing_dispatched(self):
        for mock in self.handlers.values():
            mock.assert_not_called()


class TestSignedDispatch(_Base):
    def test_signed_event_reaches_its_channel_handler(self):
        for channel, (_, takes_headers) in CHANNEL_HANDLERS.items():
            with self.subTest(channel=channel):
                headers = {"x-slack-request-timestamp": "1"} if takes_headers else {}
                auth = index._sign_async_dispatch(channel, '{"k":1}', headers)
                result = index.handler(_event(channel, '{"k":1}', headers, auth), None)
                self.assertEqual(result["statusCode"], 200)
                if takes_headers:
                    self.handlers[channel].assert_called_once_with('{"k":1}', headers)
                else:
                    self.handlers[channel].assert_called_once_with('{"k":1}')

    def test_unsigned_event_is_not_dispatched(self):
        for channel in CHANNEL_HANDLERS:
            with self.subTest(channel=channel):
                result = index.handler(_event(channel), None)
                self.assertEqual(result["statusCode"], 401)
        self.assert_nothing_dispatched()

    def test_tampered_fields_are_rejected(self):
        auth = index._sign_async_dispatch("slack", '{"user":"a"}', {"x-slack-signature": "v0=1"})
        cases = {
            "body": _event("slack", '{"user":"b"}', {"x-slack-signature": "v0=1"}, auth),
            "channel": _event("telegram", '{"user":"a"}', {"x-slack-signature": "v0=1"}, auth),
            "headers": _event("slack", '{"user":"a"}', {"x-slack-signature": "v0=2"}, auth),
            "mac": _event("slack", '{"user":"a"}', {"x-slack-signature": "v0=1"},
                          {**auth, "mac": "0" * 64}),
        }
        for name, event in cases.items():
            with self.subTest(field=name):
                self.assertEqual(index.handler(event, None)["statusCode"], 401)
        self.assert_nothing_dispatched()

    def test_malformed_authenticator_is_rejected(self):
        good = index._sign_async_dispatch("line", "{}", {})
        for auth in ("x", [], {}, {**good, "v": 2}, {**good, "ts": str(good["ts"])},
                     {**good, "mac": None}):
            with self.subTest(auth=auth):
                self.assertEqual(index.handler(_event("line", auth=auth), None)["statusCode"], 401)
        self.assert_nothing_dispatched()

    def test_key_from_another_secret_is_rejected(self):
        self.secret.return_value = "some-other-secret"
        auth = index._sign_async_dispatch("discord", "{}", {})
        self.secret.return_value = WEBHOOK_SECRET
        self.assertEqual(index.handler(_event("discord", auth=auth), None)["statusCode"], 401)
        self.assert_nothing_dispatched()

    def test_missing_webhook_secret_fails_closed(self):
        auth = index._sign_async_dispatch("telegram", "{}", {})
        self.secret.return_value = ""
        self.assertEqual(index.handler(_event("telegram", auth=auth), None)["statusCode"], 401)
        self.assert_nothing_dispatched()


class TestFreshness(_Base):
    def test_window_covers_async_retries_of_a_timed_out_run(self):
        self.assertGreaterEqual(index._ASYNC_DISPATCH_MAX_AGE_SECONDS, 3 * index.LAMBDA_TIMEOUT_SECONDS)

    def test_retry_inside_window_is_accepted(self):
        auth = index._sign_async_dispatch("telegram", "{}", {})
        later = time.time() + index._ASYNC_DISPATCH_MAX_AGE_SECONDS - 5
        with patch.object(index.time, "time", return_value=later):
            self.assertEqual(index.handler(_event("telegram", auth=auth), None)["statusCode"], 200)
        self.handlers["telegram"].assert_called_once()

    def test_event_older_than_window_is_rejected(self):
        auth = index._sign_async_dispatch("telegram", "{}", {})
        later = time.time() + index._ASYNC_DISPATCH_MAX_AGE_SECONDS + 5
        with patch.object(index.time, "time", return_value=later):
            self.assertEqual(index.handler(_event("telegram", auth=auth), None)["statusCode"], 401)
        self.assert_nothing_dispatched()


class TestSecretRotation(unittest.TestCase):
    """A container holding a stale cached webhook secret re-reads it once."""

    @patch.object(index, "handle_telegram")
    def test_stale_cached_secret_is_refreshed_once(self, handle):
        index._token_cache.clear()
        with patch.object(index, "_get_webhook_secret", return_value="rotated-secret"):
            auth = index._sign_async_dispatch("telegram", "{}", {})
        index._token_cache[index.WEBHOOK_SECRET_ID] = ("old-secret", time.time())
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {"SecretString": "rotated-secret"}
        with patch.object(index, "WEBHOOK_SECRET_ID", "openclaw/webhook-secret"), \
                patch.object(index, "secrets_client", secrets_client):
            index._token_cache[index.WEBHOOK_SECRET_ID] = ("old-secret", time.time())
            result = index.handler(_event("telegram", auth=auth), None)
        self.assertEqual(result["statusCode"], 200)
        handle.assert_called_once_with("{}")
        secrets_client.get_secret_value.assert_called_once()
        index._token_cache.clear()


class TestSelfInvokeRoundTrip(_Base):
    def _captured_payload(self, channel, body, headers):
        client = MagicMock()
        with patch.object(index, "lambda_client", client):
            index._self_invoke_async(channel, body, headers)
        client.invoke.assert_called_once()
        kwargs = client.invoke.call_args.kwargs
        self.assertEqual(kwargs["InvocationType"], "Event")
        return json.loads(kwargs["Payload"])

    def test_every_channel_round_trips_through_the_signed_envelope(self):
        raw_headers = {"x-slack-signature": "v0=1", "x-lark-signature": "s",
                       "x-telegram-bot-api-secret-token": WEBHOOK_SECRET, "user-agent": "x"}
        for channel, (_, takes_headers) in CHANNEL_HANDLERS.items():
            with self.subTest(channel=channel):
                payload = self._captured_payload(channel, '{"m":"hi"}', raw_headers)
                # Only Slack/Feishu headers are forwarded; the Telegram secret header never is.
                self.assertEqual(payload["_headers"], {"x-slack-signature": "v0=1", "x-lark-signature": "s"})
                self.assertNotIn(WEBHOOK_SECRET, json.dumps(payload))
                self.assertEqual(index.handler(payload, None)["statusCode"], 200)
                self.handlers[channel].assert_called_once()

    def test_no_invoke_without_a_signing_key(self):
        self.secret.return_value = ""
        client = MagicMock()
        with patch.object(index, "lambda_client", client):
            index._self_invoke_async("telegram", "{}", {})
        client.invoke.assert_not_called()

    def test_verified_telegram_webhook_is_dispatched_end_to_end(self):
        client = MagicMock()
        api_event = {
            "requestContext": {"http": {"method": "POST", "path": "/webhook/telegram"}},
            "headers": {"x-telegram-bot-api-secret-token": WEBHOOK_SECRET},
            "body": '{"update_id": 1}',
        }
        with patch.object(index, "lambda_client", client):
            self.assertEqual(index.handler(api_event, None)["statusCode"], 200)
        payload = json.loads(client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(index.handler(payload, None)["statusCode"], 200)
        self.handlers["telegram"].assert_called_once_with('{"update_id": 1}')


if __name__ == "__main__":
    unittest.main()
