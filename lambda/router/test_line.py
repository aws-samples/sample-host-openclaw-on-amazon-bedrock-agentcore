"""Tests for the LINE Messaging API channel in the Router Lambda.

All IDs, tokens and secrets below are synthetic placeholders.
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ["LINE_TOKEN_SECRET_ID"] = "openclaw/channels/line"

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")
line_channel = importlib.import_module("line_channel")
index.LINE_TOKEN_SECRET_ID = "openclaw/channels/line"

CHANNEL_SECRET = "0123456789abcdef0123456789abcdef"
ACCESS_TOKEN = "synthetic-channel-access-token"
USER_ID = "U" + "0" * 31 + "1"
BOT_ID = "U" + "f" * 32
REPLY_TOKEN = "nHuyWiB7yP5Zw52FIkcQobQuGDXCTA"
SECRET = json.dumps({"channelSecret": CHANNEL_SECRET, "channelAccessToken": ACCESS_TOKEN})
CONFIG = line_channel.parse_config(SECRET)
ROUTE = SimpleNamespace(platform_version="V2", user_id="user_abc123", actor_id=f"line:{USER_ID}")


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _event(text="Hello", event_id="01HSYNTHETICEVENT0000000001", ts=None, msg_type="text",
           source=None, redelivery=False, mode="active", message_id="100000000000000001", provider="line"):
    message = {"id": message_id, "type": msg_type, "quoteToken": "q"}
    if msg_type == "text":
        message["text"] = text
    elif msg_type == "image":
        message["contentProvider"] = {"type": provider}
    return {
        "type": "message", "mode": mode,
        "timestamp": int((ts if ts is not None else time.time()) * 1000),
        "source": source or {"type": "user", "userId": USER_ID},
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": redelivery},
        "replyToken": REPLY_TOKEN,
        "message": message,
    }


def _payload(*events, destination=BOT_ID):
    return {"destination": destination, "events": list(events)}


def _sign(body, secret=CHANNEL_SECRET):
    return base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _api_event(body, signature=None):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["x-line-signature"] = signature
    return {"requestContext": {"http": {"method": "POST", "path": "/webhook/line", "sourceIp": "192.0.2.1"}},
            "rawPath": "/webhook/line", "headers": headers, "body": body, "isBase64Encoded": False}


class TestParseConfig(unittest.TestCase):
    def test_complete(self):
        self.assertEqual(CONFIG["channelSecret"], CHANNEL_SECRET)
        self.assertEqual(CONFIG["botUserId"], "")

    def test_placeholder_and_incomplete_disable(self):
        for raw in ("q8YH2kTn0WmZp4rLx7Vb3sDf9GhJ1cNe", "disabled-placeholder", "", "[]",
                    json.dumps({"channelSecret": "x"}), json.dumps({"channelAccessToken": "x"}),
                    json.dumps({"channelSecret": "x", "channelAccessToken": "y", "botUserId": "bad"})):
            self.assertIsNone(line_channel.parse_config(raw), raw)


class TestSignature(unittest.TestCase):
    def test_valid(self):
        body = json.dumps(_payload(_event()))
        self.assertTrue(line_channel.verify_signature({"x-line-signature": _sign(body)}, body.encode(), CHANNEL_SECRET))

    def test_missing(self):
        self.assertFalse(line_channel.verify_signature({}, b"{}", CHANNEL_SECRET))

    def test_wrong_secret(self):
        body = "{}"
        self.assertFalse(line_channel.verify_signature({"x-line-signature": _sign(body, "other")}, body.encode(), CHANNEL_SECRET))

    def test_tampered_body(self):
        body = json.dumps(_payload(_event("hello")))
        sig = _sign(body)
        self.assertFalse(line_channel.verify_signature({"x-line-signature": sig}, body.replace("hello", "hellp").encode(), CHANNEL_SECRET))

    def test_malformed_and_hex_signature(self):
        body = "{}"
        hex_sig = hmac.new(CHANNEL_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        for sig in ("not base64!", hex_sig, ""):
            self.assertFalse(line_channel.verify_signature({"x-line-signature": sig}, body.encode(), CHANNEL_SECRET))

    def test_no_secret_fails_closed(self):
        self.assertFalse(line_channel.verify_signature({"x-line-signature": _sign("{}", "")}, b"{}", ""))


class TestEntry(unittest.TestCase):
    def setUp(self):
        index._token_cache.clear()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value="disabled-placeholder")
    def test_disabled_404(self, _s, mock_async):
        body = json.dumps(_payload(_event()))
        self.assertEqual(index.handler(_api_event(body, _sign(body)), None)["statusCode"], 404)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_missing_bad_tampered_401(self, _s, mock_async):
        body = json.dumps(_payload(_event("hello")))
        for event in (_api_event(body), _api_event(body, _sign(body, "wrong")),
                      _api_event(body.replace("hello", "hellp"), _sign(body))):
            self.assertEqual(index.handler(event, None)["statusCode"], 401)
        mock_async.assert_not_called()

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_body_not_parsed_before_verify(self, _s):
        real_loads = json.loads
        body = '{"destination": "x", "events": []}'

        def guarded(value, *a, **kw):
            if value == body:
                raise AssertionError("body parsed before signature check")
            return real_loads(value, *a, **kw)
        with patch.object(index.json, "loads", side_effect=guarded):
            self.assertEqual(index.handler(_api_event(body, _sign(body, "wrong")), None)["statusCode"], 401)

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_valid_dispatches(self, _s, mock_async):
        body = json.dumps(_payload(_event()))
        self.assertEqual(index.handler(_api_event(body, _sign(body)), None)["statusCode"], 200)
        mock_async.assert_called_once_with("line", body, {})

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_console_verify_request(self, _s, mock_async):
        body = json.dumps({"destination": BOT_ID, "events": []})
        self.assertEqual(index.handler(_api_event(body, _sign(body)), None)["statusCode"], 200)

    @patch.object(index, "handle_line")
    def test_async_dispatch(self, mock_handle):
        index.handler({"_async_dispatch": True, "_channel": "line", "_body": "{}", "_headers": {}}, None)
        mock_handle.assert_called_once_with("{}")


class TestExtract(unittest.TestCase):
    def test_text(self):
        events = line_channel.extract_events(_payload(_event("hi")))
        self.assertEqual([(e["user_id"], e["text"], e["reply_token"]) for e in events], [(USER_ID, "hi", REPLY_TOKEN)])

    def test_replay_window(self):
        self.assertEqual(line_channel.extract_events(_payload(_event(ts=time.time() - 600))), [])

    def test_redelivery_gets_longer_window(self):
        events = line_channel.extract_events(_payload(_event(ts=time.time() - 600, redelivery=True)))
        self.assertTrue(events[0]["redelivery"])
        self.assertEqual(line_channel.extract_events(_payload(_event(ts=time.time() - 7200, redelivery=True))), [])

    def test_groups_standby_and_unsupported_dropped(self):
        for ev in (_event(source={"type": "group", "groupId": "C" + "0" * 32, "userId": USER_ID}),
                   _event(mode="standby"), _event(msg_type="sticker"),
                   _event(source={"type": "user", "userId": "not-a-line-id"}),
                   {**_event(), "type": "follow"}):
            self.assertEqual(line_channel.extract_events(_payload(ev)), [], ev)

    def test_destination_checked_when_configured(self):
        self.assertEqual(line_channel.extract_events(_payload(_event(), destination="U" + "a" * 32), BOT_ID), [])
        self.assertEqual(len(line_channel.extract_events(_payload(_event()), BOT_ID)), 1)

    def test_image(self):
        events = line_channel.extract_events(_payload(_event(msg_type="image")))
        self.assertEqual(events[0]["message_id"], "100000000000000001")
        self.assertEqual(line_channel.extract_events(_payload(_event(msg_type="image", provider="external"))), [])


@patch.object(index, "_get_secret", return_value=SECRET)
@patch.object(index, "_claim_line_event", return_value=True)
@patch.object(index.line_channel, "show_loading")
@patch.object(index.line_channel, "send_text", return_value=True)
class TestHandleLine(unittest.TestCase):

    @patch.object(index, "get_model_override", return_value=None)
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi there"})
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_happy_path(self, mock_resolve, _sess, mock_auth, mock_invoke, _m, mock_send, mock_loading, _c, _s):
        index.handle_line(json.dumps(_payload(_event("What's up?"))))
        mock_resolve.assert_called_once_with("line", USER_ID)
        mock_auth.assert_called_once()
        mock_loading.assert_called_once_with(CONFIG, USER_ID)
        self.assertEqual(mock_invoke.call_args.args[3:5], ("line", "What's up?"))
        self.assertEqual(mock_invoke.call_args.kwargs["channel_target"], USER_ID)
        mock_send.assert_called_once_with(CONFIG, USER_ID, "Hi there", REPLY_TOKEN)

    @patch.object(index, "invoke_agent_runtime")
    @patch.object(index, "resolve_user", return_value=(None, False))
    def test_not_allowlisted(self, _r, mock_invoke, mock_send, _l, _c, _s):
        index.handle_line(json.dumps(_payload(_event())))
        mock_invoke.assert_not_called()
        self.assertIn(f"line:{USER_ID}", mock_send.call_args.args[2])
        self.assertEqual(mock_send.call_args.args[3], REPLY_TOKEN)

    @patch.object(index, "resolve_user")
    def test_duplicate_event(self, mock_resolve, mock_send, _l, mock_claim, _s):
        mock_claim.return_value = False
        index.handle_line(json.dumps(_payload(_event())))
        mock_resolve.assert_not_called()
        mock_send.assert_not_called()

    @patch.object(index, "redeem_bind_code", return_value=("user_abc123", True))
    @patch.object(index, "resolve_user")
    def test_bind_before_allowlist(self, mock_resolve, mock_redeem, mock_send, _l, _c, _s):
        index.handle_line(json.dumps(_payload(_event("link ABCD1234"))))
        mock_redeem.assert_called_once_with("ABCD1234", "line", USER_ID)
        mock_resolve.assert_not_called()

    @patch.object(index, "get_model_override", return_value=None)
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "A cat"})
    @patch.object(index, "_upload_image_to_s3", return_value=f"line_{USER_ID}/_uploads/img.jpeg")
    @patch.object(index.line_channel, "download_image", return_value=(b"jpeg", "image/jpeg"))
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_image(self, _r, _sess, _a, mock_dl, mock_up, mock_invoke, _m, mock_send, _l, _c, _s):
        index.handle_line(json.dumps(_payload(_event(msg_type="image"))))
        mock_dl.assert_called_once_with(CONFIG, "100000000000000001", index.ALLOWED_IMAGE_TYPES, index.MAX_IMAGE_BYTES)
        mock_up.assert_called_once_with(b"jpeg", f"line_{USER_ID}", "image/jpeg")
        self.assertEqual(mock_invoke.call_args.args[4]["text"], "What is this image?")

    @patch.object(index, "invoke_agent_runtime")
    def test_unconfigured_drops(self, mock_invoke, mock_send, _l, _c, mock_secret):
        mock_secret.return_value = "disabled-placeholder"
        index.handle_line(json.dumps(_payload(_event())))
        mock_invoke.assert_not_called()
        mock_send.assert_not_called()


class TestSend(unittest.TestCase):
    def _http_error(self, code):
        return HTTPError("https://api.line.me/v2/bot/message/reply", code, "err", {}, io.BytesIO(b""))

    def test_reply_then_extra_batches_via_push(self):
        with patch.object(line_channel.urllib_request, "urlopen") as mock_open:
            self.assertTrue(line_channel.send_text(CONFIG, USER_ID, "a" * (4900 * 6), REPLY_TOKEN))
        reqs = [c.args[0] for c in mock_open.call_args_list]
        self.assertTrue(reqs[0].full_url.endswith("/message/reply"))
        self.assertEqual(json.loads(reqs[0].data)["replyToken"], REPLY_TOKEN)
        self.assertEqual(len(json.loads(reqs[0].data)["messages"]), 5)
        self.assertTrue(reqs[1].full_url.endswith("/message/push"))
        self.assertEqual(json.loads(reqs[1].data)["to"], USER_ID)
        self.assertTrue(reqs[1].get_header("X-line-retry-key"))
        self.assertEqual(reqs[0].get_header("Authorization"), f"Bearer {ACCESS_TOKEN}")

    def test_expired_reply_token_falls_back_to_push(self):
        responses = [self._http_error(400), MagicMock()]
        with patch.object(line_channel.urllib_request, "urlopen", side_effect=responses) as mock_open:
            self.assertTrue(line_channel.send_text(CONFIG, USER_ID, "late reply", REPLY_TOKEN))
        urls = [c.args[0].full_url for c in mock_open.call_args_list]
        self.assertEqual([u.rsplit("/", 1)[-1] for u in urls], ["reply", "push"])

    def test_other_reply_error_does_not_push(self):
        with patch.object(line_channel.urllib_request, "urlopen", side_effect=[self._http_error(401)]) as mock_open:
            self.assertFalse(line_channel.send_text(CONFIG, USER_ID, "x", REPLY_TOKEN))
        self.assertEqual(mock_open.call_count, 1)

    def test_no_reply_token_uses_push(self):
        with patch.object(line_channel.urllib_request, "urlopen") as mock_open:
            self.assertTrue(line_channel.send_text(CONFIG, USER_ID, "cron result"))
        self.assertTrue(mock_open.call_args.args[0].full_url.endswith("/message/push"))

    def test_bad_recipient(self):
        with patch.object(line_channel.urllib_request, "urlopen") as mock_open:
            self.assertFalse(line_channel.send_text(CONFIG, "../x", "x"))
        mock_open.assert_not_called()

    def test_download_image_type_and_size(self):
        def resp(ctype, data):
            r = MagicMock()
            r.__enter__.return_value = r
            r.headers = {"Content-Type": ctype}
            r.read.return_value = data
            return r
        with patch.object(line_channel.urllib_request, "urlopen", return_value=resp("image/jpeg", b"jpeg")) as op:
            self.assertEqual(line_channel.download_image(CONFIG, "100000000000000001", {"image/jpeg"}, 100), (b"jpeg", "image/jpeg"))
        self.assertEqual(op.call_args.args[0].full_url,
                         "https://api-data.line.me/v2/bot/message/100000000000000001/content")
        with patch.object(line_channel.urllib_request, "urlopen", return_value=resp("video/mp4", b"x")):
            self.assertEqual(line_channel.download_image(CONFIG, "100000000000000001", {"image/jpeg"}, 100), (None, None))
        with patch.object(line_channel.urllib_request, "urlopen", return_value=resp("image/jpeg", b"x" * 101)):
            self.assertEqual(line_channel.download_image(CONFIG, "100000000000000001", {"image/jpeg"}, 100), (None, None))
        self.assertEqual(line_channel.download_image(CONFIG, "../1", {"image/jpeg"}, 100), (None, None))


class TestDedupe(unittest.TestCase):
    def test_claim(self):
        with patch.object(index, "ClientError", FakeClientError), patch.object(index, "identity_table") as table:
            self.assertTrue(index._claim_line_event("01HSYNTHETICEVENT0000000001"))
            self.assertEqual(table.put_item.call_args.kwargs["Item"]["PK"], "DEDUP#line:01HSYNTHETICEVENT0000000001")
            table.put_item.side_effect = FakeClientError("ConditionalCheckFailedException")
            self.assertFalse(index._claim_line_event("01HSYNTHETICEVENT0000000001"))


class TestNoSecretsInLogs(unittest.TestCase):
    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_logs(self, _s, _a):
        body = json.dumps(_payload(_event("private line message")))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().warning("start")
            index.handler(_api_event(body, _sign(body, "bad")), None)
            with patch.object(line_channel.urllib_request, "urlopen", side_effect=OSError("boom " + ACCESS_TOKEN)):
                line_channel.send_text(CONFIG, USER_ID, "reply", REPLY_TOKEN)
        text = "\n".join(logs.output)
        for secret in (CHANNEL_SECRET, ACCESS_TOKEN, REPLY_TOKEN, "private line message"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
