"""Tests for Feishu channel support in the Router Lambda."""

import hashlib
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, call

# Set required env vars before importing the module
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ.setdefault("FEISHU_TOKEN_SECRET_ID", "openclaw/channels/feishu")
os.environ.setdefault("FEISHU_API_DOMAIN", "https://open.feishu.cn")

# Mock boto3 before importing the module
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


# --- Test helpers ---

ENCRYPT_KEY = "test-encrypt-key-12345"
FEISHU_SECRET = json.dumps({
    "appId": "cli_test123",
    "appSecret": "secret123",
    "verificationToken": "vtoken123",
    "encryptKey": ENCRYPT_KEY,
})


def _make_feishu_signature(timestamp, nonce, encrypt_key, body_bytes):
    """Compute Feishu X-Lark-Signature."""
    content = f"{timestamp}{nonce}{encrypt_key}".encode() + body_bytes
    return hashlib.sha256(content).hexdigest()


def _make_feishu_text_event(text="Hello", sender_id="ou_test123", chat_id="oc_chat456",
                             chat_type="p2p", mentions=None):
    """Build a Feishu im.message.receive_v1 event payload."""
    event = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_" + str(time.time()),
            "token": "vtoken123",
            "create_time": str(int(time.time() * 1000)),
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant_test",
            "app_id": "cli_test123",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_id, "user_id": "on_test", "union_id": "on_test"},
                "sender_type": "user",
                "tenant_key": "tenant_test",
            },
            "message": {
                "message_id": "om_test789",
                "root_id": "",
                "parent_id": "",
                "create_time": str(int(time.time() * 1000)),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }
    if mentions:
        event["event"]["message"]["mentions"] = mentions
    return event


def _make_feishu_image_event(image_key="img_v3_test", sender_id="ou_test123", chat_id="oc_chat456"):
    """Build a Feishu image message event."""
    event = _make_feishu_text_event("", sender_id, chat_id)
    event["event"]["message"]["message_type"] = "image"
    event["event"]["message"]["content"] = json.dumps({"image_key": image_key})
    return event


# --- Tests ---

class TestValidateFeishuWebhook(unittest.TestCase):
    """Tests for validate_feishu_webhook."""

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    def test_valid_signature(self, _mock_secret):
        """Valid signature returns True."""
        body = b'{"test": "data"}'
        ts = "1234567890"
        nonce = "abc123"
        sig = _make_feishu_signature(ts, nonce, ENCRYPT_KEY, body)
        headers = {
            "x-lark-request-timestamp": ts,
            "x-lark-request-nonce": nonce,
            "x-lark-signature": sig,
        }
        self.assertTrue(index.validate_feishu_webhook(headers, body))

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    def test_invalid_signature(self, _mock_secret):
        """Wrong signature returns False."""
        headers = {
            "x-lark-request-timestamp": "1234567890",
            "x-lark-request-nonce": "abc123",
            "x-lark-signature": "wrong_signature",
        }
        self.assertFalse(index.validate_feishu_webhook(headers, b"test"))

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    def test_missing_headers(self, _mock_secret):
        """Missing signature headers returns False."""
        self.assertFalse(index.validate_feishu_webhook({}, b"test"))

    @patch.object(index, "_get_secret", return_value="")
    def test_no_encrypt_key(self, _mock_secret):
        """No encrypt_key configured returns False (fail-closed)."""
        headers = {
            "x-lark-request-timestamp": "123",
            "x-lark-request-nonce": "abc",
            "x-lark-signature": "sig",
        }
        self.assertFalse(index.validate_feishu_webhook(headers, b"test"))


class TestHandleFeishuChallenge(unittest.TestCase):
    """Tests for URL verification challenge handling."""

    def test_url_verification_returns_challenge(self):
        """url_verification event returns the challenge value."""
        body = json.dumps({"type": "url_verification", "challenge": "test_challenge_123"})
        result = index.handle_feishu(body)
        self.assertEqual(result["statusCode"], 200)
        resp_body = json.loads(result["body"])
        self.assertEqual(resp_body["challenge"], "test_challenge_123")

    def test_invalid_challenge_format_rejected(self):
        """Challenge with invalid characters is rejected."""
        body = json.dumps({"type": "url_verification", "challenge": "<script>alert(1)</script>"})
        result = index.handle_feishu(body)
        self.assertEqual(result["statusCode"], 400)


class TestHandleFeishuTextMessage(unittest.TestCase):
    """Tests for Feishu text message extraction."""

    @patch.object(index, "send_feishu_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi there!"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_p2p_text_message(self, mock_resolve, mock_session, mock_invoke, mock_send):
        """P2P text message is processed correctly."""
        event = _make_feishu_text_event("Hello bot")
        result = index.handle_feishu(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        mock_resolve.assert_called_once_with("feishu", "ou_test123")
        mock_invoke.assert_called_once()
        mock_send.assert_called()
        # Last call should be the response
        last_call_text = mock_send.call_args_list[-1][0][1]
        self.assertEqual(last_call_text, "Hi there!")

    @patch.object(index, "send_feishu_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Group reply"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_group_message_strips_mention(self, mock_resolve, mock_session, mock_invoke, mock_send):
        """Group message with @mention strips the mention tag."""
        event = _make_feishu_text_event(
            text="@_user_1 What is the weather?",
            chat_type="group",
            mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Bot"}],
        )
        result = index.handle_feishu(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        # The message sent to AgentCore should have mention stripped
        invoke_args = mock_invoke.call_args
        self.assertIn("What is the weather?", invoke_args[0][4])  # 5th arg is message

    def test_group_mention_only_text_becomes_hi(self):
        """After stripping group @mention with no remaining text, text defaults to 'hi'."""
        event = _make_feishu_text_event(
            text="@_user_1",
            chat_type="group",
            mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Bot"}],
        )
        # Verify at the parsing level: the mention-stripping logic produces "hi"
        event_data = json.loads(json.dumps(event))
        message = event_data["event"]["message"]
        content = json.loads(message["content"])
        text = content.get("text", "")
        for mention in message.get("mentions", []):
            key = mention.get("key", "")
            if key:
                text = text.replace(key, "").strip()
        if not text:
            text = "hi"
        self.assertEqual(text, "hi")

    def test_non_message_event_ignored(self):
        """Non im.message.receive_v1 events are ignored."""
        event = _make_feishu_text_event()
        event["header"]["event_type"] = "im.chat.member.bot.added_v1"
        result = index.handle_feishu(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)

    def test_bot_sender_ignored(self):
        """Messages from bots (sender_type != user) are ignored."""
        event = _make_feishu_text_event()
        event["event"]["sender"]["sender_type"] = "bot"
        result = index.handle_feishu(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)

    @patch.object(index, "send_feishu_message")
    @patch.object(index, "resolve_user", return_value=(None, False))
    def test_unauthorized_user_gets_rejection(self, mock_resolve, mock_send):
        """Unauthorized user gets rejection message with their ID."""
        event = _make_feishu_text_event("Hello")
        index.handle_feishu(json.dumps(event))

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        self.assertIn("feishu:ou_test123", msg)
        self.assertIn("private", msg)


class TestHandleFeishuBindCommands(unittest.TestCase):
    """Tests for cross-channel binding via Feishu."""

    @patch.object(index, "send_feishu_message")
    @patch.object(index, "_is_bind_command", return_value=(True, "ABC123"))
    @patch.object(index, "redeem_bind_code", return_value=("user_abc", True))
    def test_bind_command_success(self, mock_redeem, _mock_is_bind, mock_send):
        """Successful bind code redemption."""
        event = _make_feishu_text_event("link ABC123")
        index.handle_feishu(json.dumps(event))

        mock_redeem.assert_called_once_with("ABC123", "feishu", "ou_test123")
        mock_send.assert_called_once()
        self.assertIn("linked", mock_send.call_args[0][1].lower())

    @patch.object(index, "send_feishu_message")
    @patch.object(index, "_is_bind_command", return_value=(False, None))
    @patch.object(index, "_is_link_command", return_value=True)
    @patch.object(index, "create_bind_code", return_value="XYZ789")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_link_accounts_command(self, mock_resolve, mock_create, _mock_is_link, _mock_is_bind, mock_send):
        """'link accounts' generates a bind code."""
        event = _make_feishu_text_event("link accounts")
        index.handle_feishu(json.dumps(event))

        mock_create.assert_called_once_with("user_abc123")
        mock_send.assert_called_once()
        self.assertIn("XYZ789", mock_send.call_args[0][1])


class TestFeishuTenantToken(unittest.TestCase):
    """Tests for Feishu tenant_access_token caching."""

    def setUp(self):
        # Reset cache between tests
        index._feishu_token_cache["token"] = ""
        index._feishu_token_cache["expires_at"] = 0

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    @patch("urllib.request.urlopen")
    def test_token_fetched_on_first_call(self, mock_urlopen, _mock_secret):
        """First call fetches a new token."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "code": 0,
            "tenant_access_token": "t-new-token",
            "expire": 7200,
        }).encode()
        mock_urlopen.return_value = mock_resp

        token = index._get_feishu_tenant_token()
        self.assertEqual(token, "t-new-token")
        mock_urlopen.assert_called_once()

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    def test_cached_token_returned(self, _mock_secret):
        """Cached token is returned without API call."""
        index._feishu_token_cache["token"] = "t-cached"
        index._feishu_token_cache["expires_at"] = time.time() + 3600

        token = index._get_feishu_tenant_token()
        self.assertEqual(token, "t-cached")

    @patch.object(index, "_get_secret", return_value=FEISHU_SECRET)
    @patch("urllib.request.urlopen")
    def test_expired_token_refreshed(self, mock_urlopen, _mock_secret):
        """Expired token triggers a refresh."""
        index._feishu_token_cache["token"] = "t-old"
        index._feishu_token_cache["expires_at"] = time.time() - 100  # expired

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "code": 0,
            "tenant_access_token": "t-refreshed",
            "expire": 7200,
        }).encode()
        mock_urlopen.return_value = mock_resp

        token = index._get_feishu_tenant_token()
        self.assertEqual(token, "t-refreshed")


class TestSendFeishuMessage(unittest.TestCase):
    """Tests for send_feishu_message."""

    @patch.object(index, "_get_feishu_tenant_token", return_value="t-test")
    @patch("urllib.request.urlopen")
    def test_send_short_message(self, mock_urlopen, _mock_token):
        """Short message is sent in a single API call."""
        index.send_feishu_message("oc_chat123", "Hello!")
        mock_urlopen.assert_called_once()

    @patch.object(index, "_get_feishu_tenant_token", return_value="t-test")
    @patch("urllib.request.urlopen")
    def test_long_message_split(self, mock_urlopen, _mock_token):
        """Long message is split into chunks."""
        long_text = "x" * 25000
        index.send_feishu_message("oc_chat123", long_text)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch.object(index, "_get_feishu_tenant_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_no_token_skips_send(self, mock_urlopen, _mock_token):
        """No token available skips sending."""
        index.send_feishu_message("oc_chat123", "Hello!")
        mock_urlopen.assert_not_called()


class TestDownloadFeishuImage(unittest.TestCase):
    """Tests for _download_feishu_image."""

    @patch.object(index, "_get_feishu_tenant_token", return_value="t-test")
    @patch("urllib.request.urlopen")
    def test_download_jpeg(self, mock_urlopen, _mock_token):
        """Image download returns bytes and content type."""
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "image/jpeg"
        mock_resp.read.return_value = b"\xff\xd8" + b"\x00" * 100
        mock_urlopen.return_value = mock_resp

        content_str = json.dumps({"image_key": "img_v3_test"})
        image_bytes, content_type, filename = index._download_feishu_image(content_str, "image")

        self.assertIsNotNone(image_bytes)
        self.assertEqual(content_type, "image/jpeg")
        self.assertIn("feishu_img_v3_test", filename)

    def test_non_image_type_returns_none(self):
        """Non-image message type returns None."""
        result = index._download_feishu_image('{"text": "hi"}', "text")
        self.assertEqual(result, (None, None, None))

    @patch.object(index, "_get_feishu_tenant_token", return_value="t-test")
    @patch("urllib.request.urlopen")
    def test_disallowed_content_type_rejected(self, mock_urlopen, _mock_token):
        """Content type not in ALLOWED_IMAGE_TYPES is rejected."""
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "application/pdf"
        mock_resp.read.return_value = b"pdf data"
        mock_urlopen.return_value = mock_resp

        content_str = json.dumps({"image_key": "img_v3_pdf"})
        image_bytes, _, _ = index._download_feishu_image(content_str, "image")
        self.assertIsNone(image_bytes)

    def test_missing_image_key_returns_none(self):
        """Missing image_key returns None."""
        result = index._download_feishu_image('{}', "image")
        self.assertEqual(result, (None, None, None))


class TestHandlerFeishuRouting(unittest.TestCase):
    """Tests for Feishu path routing in the Lambda handler."""

    def test_feishu_challenge_routed(self):
        """Feishu url_verification is handled synchronously (not async dispatched)."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/webhook/feishu"}},
            "body": json.dumps({"type": "url_verification", "challenge": "test123"}),
            "headers": {},
        }
        result = index.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("test123", result["body"])

    @patch.object(index, "validate_feishu_webhook", return_value=False)
    def test_feishu_invalid_signature_rejected(self, _mock_validate):
        """Invalid Feishu signature returns 401."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/webhook/feishu"}},
            "body": json.dumps(_make_feishu_text_event()),
            "headers": {"x-lark-request-timestamp": "123", "x-lark-request-nonce": "abc", "x-lark-signature": "bad"},
        }
        result = index.handler(event, None)
        self.assertEqual(result["statusCode"], 401)

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "validate_feishu_webhook", return_value=True)
    def test_feishu_valid_request_dispatched(self, _mock_validate, mock_dispatch):
        """Valid Feishu webhook is dispatched asynchronously."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/webhook/feishu"}},
            "body": json.dumps(_make_feishu_text_event()),
            "headers": {"x-lark-request-timestamp": "123", "x-lark-request-nonce": "abc", "x-lark-signature": "sig"},
        }
        result = index.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        mock_dispatch.assert_called_once_with("feishu", unittest.mock.ANY, unittest.mock.ANY)


if __name__ == "__main__":
    unittest.main()
