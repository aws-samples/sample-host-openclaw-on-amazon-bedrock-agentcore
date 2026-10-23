"""Tests for the WhatsApp Cloud API channel in the Router Lambda.

All IDs, tokens and secrets below are synthetic placeholders.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ["WHATSAPP_TOKEN_SECRET_ID"] = "openclaw/channels/whatsapp"

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")
whatsapp = importlib.import_module("whatsapp")
index.WHATSAPP_TOKEN_SECRET_ID = "openclaw/channels/whatsapp"

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ACCESS_TOKEN = "EAAtest-access-token"
PHONE_NUMBER_ID = "100000000000001"
WA_ID = "15550000001"
SECRET = json.dumps({
    "accessToken": ACCESS_TOKEN,
    "appSecret": APP_SECRET,
    "verifyToken": VERIFY_TOKEN,
    "phoneNumberId": PHONE_NUMBER_ID,
})
CONFIG = whatsapp.parse_config(SECRET)
ROUTE = SimpleNamespace(platform_version="V2", user_id="user_abc123", actor_id=f"whatsapp:{WA_ID}")


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _payload(text="Hello", msg_id="wamid.TEST1", ts=None, msg_type="text",
             phone_number_id=PHONE_NUMBER_ID, sender=WA_ID, image=None):
    msg = {"from": sender, "id": msg_id, "timestamp": str(int(ts if ts is not None else time.time())),
           "type": msg_type}
    if msg_type == "text":
        msg["text"] = {"body": text}
    elif msg_type == "image":
        msg["image"] = image or {"id": "200000000000001", "mime_type": "image/jpeg", "caption": text}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "300000000000001", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
            "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
            "messages": [msg],
        }}]}],
    }


def _sign(body, secret=APP_SECRET):
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _event(body, signature=None, method="POST", query=None, path="/webhook/whatsapp"):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    ev = {"requestContext": {"http": {"method": method, "path": path, "sourceIp": "192.0.2.1"}},
          "rawPath": path, "headers": headers, "body": body, "isBase64Encoded": False}
    if query is not None:
        ev["queryStringParameters"] = query
    return ev


class TestParseConfig(unittest.TestCase):
    def test_complete_config(self):
        self.assertEqual(CONFIG["graphApiVersion"], whatsapp.DEFAULT_GRAPH_API_VERSION)

    def test_placeholder_random_string_is_disabled(self):
        self.assertIsNone(whatsapp.parse_config("q8YH2kTn0WmZp4rLx7Vb3sDf9GhJ1cNe"))

    def test_empty_and_missing_keys_disabled(self):
        self.assertIsNone(whatsapp.parse_config(""))
        self.assertIsNone(whatsapp.parse_config(json.dumps({"accessToken": "a", "appSecret": "b"})))
        self.assertIsNone(whatsapp.parse_config(json.dumps(["x"])))

    def test_bad_phone_id_or_version_disabled(self):
        bad = json.loads(SECRET)
        bad["phoneNumberId"] = "../me"
        self.assertIsNone(whatsapp.parse_config(json.dumps(bad)))
        bad = json.loads(SECRET)
        bad["graphApiVersion"] = "v23.0/../../x"
        self.assertIsNone(whatsapp.parse_config(json.dumps(bad)))


class TestSignature(unittest.TestCase):
    def test_valid(self):
        body = json.dumps(_payload())
        self.assertTrue(whatsapp.verify_signature({"x-hub-signature-256": _sign(body)}, body.encode(), APP_SECRET))

    def test_missing_header(self):
        self.assertFalse(whatsapp.verify_signature({}, b"{}", APP_SECRET))

    def test_wrong_secret(self):
        body = "{}"
        self.assertFalse(whatsapp.verify_signature({"x-hub-signature-256": _sign(body, "other")}, body.encode(), APP_SECRET))

    def test_missing_prefix(self):
        body = "{}"
        bare = _sign(body)[len("sha256="):]
        self.assertFalse(whatsapp.verify_signature({"x-hub-signature-256": bare}, body.encode(), APP_SECRET))

    def test_tampered_body(self):
        body = json.dumps(_payload())
        sig = _sign(body)
        self.assertFalse(whatsapp.verify_signature({"x-hub-signature-256": sig}, body.replace("Hello", "Hellp").encode(), APP_SECRET))

    def test_no_secret_fails_closed(self):
        self.assertFalse(whatsapp.verify_signature({"x-hub-signature-256": _sign("{}", "")}, b"{}", ""))


class TestHandlerEntry(unittest.TestCase):
    """The API Gateway entry point: disabled, bad/missing signature, happy path."""

    def setUp(self):
        index._token_cache.clear()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value="q8YH2kTn0WmZp4rLx7Vb3sDf9GhJ1cNe")
    def test_unconfigured_channel_returns_404(self, _secret, mock_async):
        body = json.dumps(_payload())
        self.assertEqual(index.handler(_event(body, _sign(body)), None)["statusCode"], 404)
        self.assertEqual(index.handler(_event("", method="GET", query={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123"}), None)["statusCode"], 404)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_missing_signature_401(self, _secret, mock_async):
        result = index.handler(_event(json.dumps(_payload())), None)
        self.assertEqual(result["statusCode"], 401)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_bad_signature_401(self, _secret, mock_async):
        body = json.dumps(_payload())
        result = index.handler(_event(body, _sign(body, "wrong-secret")), None)
        self.assertEqual(result["statusCode"], 401)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_malformed_body_with_bad_signature_never_parsed(self, _secret, mock_async):
        real_loads = json.loads
        body = '{"object": "whatsapp_business_account"}'

        def guarded(value, *a, **kw):
            if value == body:
                raise AssertionError("body parsed before signature check")
            return real_loads(value, *a, **kw)
        with patch.object(index.json, "loads", side_effect=guarded):
            result = index.handler(_event(body, "sha256=00"), None)
        self.assertEqual(result["statusCode"], 401)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_valid_signature_dispatches(self, _secret, mock_async):
        body = json.dumps(_payload())
        result = index.handler(_event(body, _sign(body)), None)
        self.assertEqual(result["statusCode"], 200)
        mock_async.assert_called_once_with("whatsapp", body, {})

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_get_verification(self, _secret):
        ok = index.handler(_event("", method="GET", query={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"}), None)
        self.assertEqual((ok["statusCode"], ok["body"]), (200, "1158201444"))
        for query in ({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
                      {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
                      {"hub.mode": "subscribe", "hub.challenge": "1"},
                      {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "<script>"},
                      {}):
            self.assertEqual(index.handler(_event("", method="GET", query=query), None)["statusCode"], 403, query)

    def test_get_on_other_paths_still_405(self):
        self.assertEqual(index.handler(_event("", method="GET", path="/webhook/telegram"), None)["statusCode"], 405)

    @patch.object(index, "handle_whatsapp")
    @patch.object(index, "_get_webhook_secret", return_value="test-webhook-secret")
    def test_async_dispatch(self, _ws, mock_handle):
        auth = index._sign_async_dispatch("whatsapp", "{}", {})
        index.handler({"_async_dispatch": True, "_channel": "whatsapp", "_body": "{}", "_headers": {}, "_auth": auth}, None)
        mock_handle.assert_called_once_with("{}")

    @patch.object(index, "handle_whatsapp")
    @patch.object(index, "_get_webhook_secret", return_value="test-webhook-secret")
    def test_async_dispatch_rejects_unsigned(self, _ws, mock_handle):
        result = index.handler({"_async_dispatch": True, "_channel": "whatsapp", "_body": "{}", "_headers": {}}, None)
        self.assertEqual(result["statusCode"], 401)
        mock_handle.assert_not_called()


class TestExtractMessages(unittest.TestCase):
    def test_text(self):
        msgs = whatsapp.extract_messages(_payload("hi"), PHONE_NUMBER_ID)
        self.assertEqual([(m["from"], m["text"], m["name"]) for m in msgs], [(WA_ID, "hi", "Test User")])

    def test_replayed_old_message_dropped(self):
        old = _payload(ts=time.time() - whatsapp.MAX_MESSAGE_AGE_SECONDS - 5)
        self.assertEqual(whatsapp.extract_messages(old, PHONE_NUMBER_ID), [])

    def test_other_phone_number_dropped(self):
        self.assertEqual(whatsapp.extract_messages(_payload(phone_number_id="999"), PHONE_NUMBER_ID), [])

    def test_unsupported_type_and_status_dropped(self):
        self.assertEqual(whatsapp.extract_messages(_payload(msg_type="sticker"), PHONE_NUMBER_ID), [])
        statuses = _payload()
        del statuses["entry"][0]["changes"][0]["value"]["messages"]
        statuses["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "x", "status": "read"}]
        self.assertEqual(whatsapp.extract_messages(statuses, PHONE_NUMBER_ID), [])

    def test_non_numeric_sender_dropped(self):
        self.assertEqual(whatsapp.extract_messages(_payload(sender="abc:def"), PHONE_NUMBER_ID), [])

    def test_image(self):
        msgs = whatsapp.extract_messages(_payload("look", msg_type="image"), PHONE_NUMBER_ID)
        self.assertEqual((msgs[0]["image_id"], msgs[0]["image_mime"], msgs[0]["text"]),
                         ("200000000000001", "image/jpeg", "look"))

    def test_split_text(self):
        chunks = whatsapp.split_text("a" * 9000)
        self.assertTrue(all(len(c) <= whatsapp.TEXT_CHUNK_CHARS for c in chunks))
        self.assertEqual("".join(chunks), "a" * 9000)


@patch.object(index, "_get_secret", return_value=SECRET)
@patch.object(index, "_claim_whatsapp_message", return_value=True)
@patch.object(index.whatsapp, "send_text", return_value=True)
class TestHandleWhatsapp(unittest.TestCase):

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi there"})
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_happy_path(self, mock_resolve, _session, mock_auth, mock_invoke, mock_send, _claim, _secret):
        index.handle_whatsapp(json.dumps(_payload("What's up?")))
        mock_resolve.assert_called_once_with("whatsapp", WA_ID, "Test User")
        mock_auth.assert_called_once()
        args = mock_invoke.call_args
        self.assertEqual(args.args[3], "whatsapp")
        self.assertEqual(args.args[4], "What's up?")
        self.assertEqual(args.kwargs["channel_target"], WA_ID)
        mock_send.assert_called_with(CONFIG, WA_ID, "Hi there")

    @patch.object(index, "invoke_agent_runtime")
    @patch.object(index, "resolve_user", return_value=(None, False))
    def test_not_allowlisted(self, _resolve, mock_invoke, mock_send, _claim, _secret):
        index.handle_whatsapp(json.dumps(_payload()))
        mock_invoke.assert_not_called()
        self.assertIn(f"whatsapp:{WA_ID}", mock_send.call_args.args[2])
        self.assertIn("requires an invitation", mock_send.call_args.args[2])

    @patch.object(index, "invoke_agent_runtime")
    @patch.object(index, "resolve_user")
    def test_replayed_message_not_processed(self, mock_resolve, mock_invoke, mock_send, _claim, _secret):
        index.handle_whatsapp(json.dumps(_payload(ts=time.time() - 3600)))
        mock_resolve.assert_not_called()
        mock_invoke.assert_not_called()

    @patch.object(index, "invoke_agent_runtime")
    @patch.object(index, "resolve_user")
    def test_duplicate_message_not_processed(self, mock_resolve, mock_invoke, mock_send, mock_claim, _secret):
        mock_claim.return_value = False
        index.handle_whatsapp(json.dumps(_payload()))
        mock_resolve.assert_not_called()
        mock_invoke.assert_not_called()

    @patch.object(index, "redeem_bind_code", return_value=("user_abc123", True))
    @patch.object(index, "resolve_user")
    def test_bind_before_allowlist(self, mock_resolve, mock_redeem, mock_send, _claim, _secret):
        index.handle_whatsapp(json.dumps(_payload("link ABCD1234")))
        mock_redeem.assert_called_once_with("ABCD1234", "whatsapp", WA_ID, "Test User")
        mock_resolve.assert_not_called()

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "A cat"})
    @patch.object(index, "_upload_image_to_s3", return_value="whatsapp_15550000001/_uploads/img.jpeg")
    @patch.object(index.whatsapp, "download_media", return_value=(b"jpegbytes", "image/jpeg"))
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_image(self, _r, _s, _a, mock_dl, mock_up, mock_invoke, mock_send, _claim, _secret):
        index.handle_whatsapp(json.dumps(_payload("", msg_type="image")))
        mock_dl.assert_called_once()
        mock_up.assert_called_once_with(b"jpegbytes", "whatsapp_15550000001", "image/jpeg")
        message = mock_invoke.call_args.args[4]
        self.assertEqual(message["images"][0]["s3Key"], "whatsapp_15550000001/_uploads/img.jpeg")

    @patch.object(index, "invoke_agent_runtime")
    def test_unconfigured_drops(self, mock_invoke, mock_send, _claim, mock_secret):
        mock_secret.return_value = "placeholder"
        index.handle_whatsapp(json.dumps(_payload()))
        mock_invoke.assert_not_called()
        mock_send.assert_not_called()


class TestDedupe(unittest.TestCase):
    def test_claim(self):
        with patch.object(index, "ClientError", FakeClientError), \
             patch.object(index, "identity_table") as table:
            table.put_item.return_value = {}
            self.assertTrue(index._claim_whatsapp_message("wamid.A"))
            item = table.put_item.call_args.kwargs["Item"]
            self.assertTrue(item["PK"].startswith("DEDUP#whatsapp:"))
            self.assertNotIn("wamid.A", item["PK"])
            table.put_item.side_effect = FakeClientError("ConditionalCheckFailedException")
            self.assertFalse(index._claim_whatsapp_message("wamid.A"))


class TestNoSecretsInLogs(unittest.TestCase):
    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_rejections_and_sends_do_not_log_secrets(self, _secret, _async):
        body = json.dumps(_payload("private message body"))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().warning("start")
            index.handler(_event(body, _sign(body, "bad")), None)
            with patch.object(whatsapp.urllib_request, "urlopen", side_effect=OSError("boom " + ACCESS_TOKEN)):
                whatsapp.send_text(CONFIG, WA_ID, "reply")
        text = "\n".join(logs.output)
        for secret in (APP_SECRET, ACCESS_TOKEN, VERIFY_TOKEN, "private message body"):
            self.assertNotIn(secret, text)


class TestMediaDownload(unittest.TestCase):
    def _meta(self, payload):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(payload).encode()
        return resp

    def test_host_allowlist(self):
        self.assertTrue(whatsapp._is_meta_media_url("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"))
        for url in ("http://lookaside.fbsbx.com/x", "https://evil.example/x", "https://fbsbx.com.evil.example/x", None):
            self.assertFalse(whatsapp._is_meta_media_url(url), url)

    def test_downloads_allowed_image(self):
        data = MagicMock()
        data.__enter__.return_value = data
        data.read.return_value = b"jpegbytes"
        meta = self._meta({"url": "https://lookaside.fbsbx.com/m?id=1", "mime_type": "image/jpeg", "file_size": 9})
        with patch.object(whatsapp.urllib_request, "urlopen", return_value=meta), \
             patch.object(whatsapp._NO_REDIRECT_OPENER, "open", return_value=data) as dl:
            out = whatsapp.download_media(CONFIG, "200000000000001", {"image/jpeg"}, 1000)
        self.assertEqual(out, (b"jpegbytes", "image/jpeg"))
        self.assertEqual(dl.call_args.args[0].get_header("Authorization"), f"Bearer {ACCESS_TOKEN}")

    def test_rejects_foreign_host_type_and_size(self):
        cases = [
            {"url": "https://evil.example/x", "mime_type": "image/jpeg", "file_size": 9},
            {"url": "https://lookaside.fbsbx.com/x", "mime_type": "application/pdf", "file_size": 9},
            {"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/jpeg", "file_size": 5000},
        ]
        for meta in cases:
            with patch.object(whatsapp.urllib_request, "urlopen", return_value=self._meta(meta)), \
                 patch.object(whatsapp._NO_REDIRECT_OPENER, "open") as dl:
                self.assertEqual(whatsapp.download_media(CONFIG, "200000000000001", {"image/jpeg"}, 1000), (None, None))
                dl.assert_not_called()

    def test_bad_media_id(self):
        self.assertEqual(whatsapp.download_media(CONFIG, "../me", {"image/jpeg"}, 1000), (None, None))


if __name__ == "__main__":
    unittest.main()
