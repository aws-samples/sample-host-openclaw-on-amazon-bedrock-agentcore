"""Tests for Slack channel support in the Router Lambda."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set required env vars before importing the module
os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ.setdefault("SLACK_TOKEN_SECRET_ID", "openclaw/channels/slack")

# Mock AWS SDK before importing the module
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


# --- Test helpers ---

SLACK_BOT_TOKEN = "xoxb-test-token-12345"
SLACK_SIGNING_SECRET = "test-signing-secret"
SLACK_SECRET = json.dumps({
    "botToken": SLACK_BOT_TOKEN,
    "signingSecret": SLACK_SIGNING_SECRET,
})


def _make_slack_message_event(text="Hello", user_id="U12345USER", channel_id="C12345CHAN",
                            subtype=None, bot_id=None, files=None):
    """Build a Slack message event payload."""
    event = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "user": user_id,
            "channel": channel_id,
            "text": text,
            "ts": "1234567890.123456",
        },
    }
    if subtype:
        event["event"]["subtype"] = subtype
    if bot_id:
        event["event"]["bot_id"] = bot_id
    if files:
        event["event"]["files"] = files
    return event


def _make_slack_app_mention_event(text="<@UBOT123> Hello", user_id="U12345USER",
                                 channel_id="C12345CHAN"):
    """Build a Slack app_mention event payload."""
    return {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": user_id,
            "channel": channel_id,
            "text": text,
            "ts": "1234567890.123456",
        },
    }


# --- Tests ---

class TestSlackEventTypeFiltering(unittest.TestCase):
    """Tests for Slack event type filtering."""

    def test_url_verification_returns_challenge(self):
        """URL verification challenge is returned."""
        body = json.dumps({
            "type": "url_verification",
            "challenge": "test-challenge-123",
        })
        result = index.handle_slack(body)
        self.assertEqual(result["statusCode"], 200)
        response_body = json.loads(result["body"])
        self.assertEqual(response_body["challenge"], "test-challenge-123")

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "get_or_create_session", return_value="ses_test")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi"})
    @patch.object(index, "send_slack_message")
    def test_message_event_is_processed(self, mock_send, mock_invoke, mock_session, mock_resolve, mock_tokens):
        """Regular message events pass the type filter."""
        event = _make_slack_message_event()
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "get_or_create_session", return_value="ses_test")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Sunny"})
    @patch.object(index, "send_slack_message")
    def test_app_mention_event_is_processed(self, mock_send, mock_invoke, mock_session, mock_resolve, mock_tokens):
        """app_mention events pass the type filter."""
        event = _make_slack_app_mention_event(text="<@UBOT123> What is the weather?")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        mock_invoke.assert_called_once()

    def test_unknown_event_type_is_ignored(self):
        """Unknown event types are ignored."""
        event = {
            "type": "event_callback",
            "event": {
                "type": "channel_created",
                "channel": {"id": "C12345"},
            },
        }
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    def test_message_with_subtype_is_ignored(self):
        """Message events with unsupported subtypes are ignored."""
        event = _make_slack_message_event(subtype="message_changed")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "get_or_create_session", return_value="ses_test")
    @patch.object(index, "_download_slack_file", return_value=(b"img", "image/jpeg", None))
    @patch.object(index, "_upload_image_to_s3", return_value="user/img.jpg")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Nice image"})
    @patch.object(index, "send_slack_message")
    def test_file_share_subtype_is_allowed(self, mock_send, mock_invoke, mock_upload, mock_download, mock_session, mock_resolve, mock_tokens):
        """file_share subtype is allowed for image uploads."""
        event = _make_slack_message_event(
            text="Check this image",
            subtype="file_share",
            files=[{"mimetype": "image/jpeg", "url_private_download": "https://example.com/img.jpg"}],
        )
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)


class TestSlackMentionStripping(unittest.TestCase):
    """Tests for bot mention stripping in Slack messages."""

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "The weather is sunny"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_app_mention_strips_bot_tag(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Bot mention tag is stripped from app_mention text."""
        event = _make_slack_app_mention_event(text="<@UBOT123> What is the weather?")
        result = index.handle_slack(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        invoke_args = mock_invoke.call_args
        message_sent = invoke_args[0][4]
        self.assertEqual(message_sent, "What is the weather?")
        self.assertNotIn("<@UBOT123>", message_sent)

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_dm_message_strips_mention_too(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Bot mention tags are also stripped from DM messages."""
        event = _make_slack_message_event(text="<@UBOT123> Hello there")
        result = index.handle_slack(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        invoke_args = mock_invoke.call_args
        message_sent = invoke_args[0][4]
        self.assertEqual(message_sent, "Hello there")

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_multiple_mentions_stripped(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Multiple bot mention tags are stripped."""
        event = _make_slack_message_event(text="<@UBOT123> <@UOTHER456> Hello")
        result = index.handle_slack(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        invoke_args = mock_invoke.call_args
        message_sent = invoke_args[0][4]
        self.assertEqual(message_sent, "Hello")

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_mention_only_text_becomes_empty(self, mock_tokens):
        """Message with only mention tag becomes empty and is rejected."""
        event = _make_slack_app_mention_event(text="<@UBOT123>")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Yes"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012")
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_message_without_mention_unchanged(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Message without mention is not affected."""
        event = _make_slack_message_event(text="Hello world")
        result = index.handle_slack(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        invoke_args = mock_invoke.call_args
        message_sent = invoke_args[0][4]
        self.assertEqual(message_sent, "Hello world")


class TestSlackBotMessageIgnored(unittest.TestCase):
    """Tests for bot message filtering."""

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_bot_message_is_ignored(self, mock_tokens):
        """Messages from bots are ignored to prevent loops."""
        event = _make_slack_message_event(bot_id="B12345BOT")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")


class TestSlackRetryHandling(unittest.TestCase):
    """Tests for Slack retry header handling."""

    def test_retry_is_ignored(self):
        """Slack retries are ignored."""
        event = _make_slack_message_event()
        headers = {"x-slack-retry-num": "1"}
        result = index.handle_slack(json.dumps(event), headers=headers)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")


class TestSlackEdgeCases(unittest.TestCase):
    """Tests for edge cases in Slack handling."""

    def test_empty_user_id_ignored(self):
        """Events without user ID are ignored."""
        event = _make_slack_message_event(user_id="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    def test_empty_channel_id_ignored(self):
        """Events without channel ID are ignored."""
        event = _make_slack_message_event(channel_id="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    def test_empty_text_without_image_ignored(self):
        """Events without text and without image are ignored."""
        event = _make_slack_message_event(text="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")


if __name__ == "__main__":
    unittest.main()
