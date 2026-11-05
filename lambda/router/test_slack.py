"""Tests for Slack channel support in the Router Lambda."""

import json
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
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
TEST_ROUTE = SimpleNamespace(platform_version="V2", user_id="user_abc123", actor_id="slack:U12345USER")
REAL_RESOLVE_USER = index.resolve_user


# --- Test helpers ---

SLACK_BOT_TOKEN = "xoxb-test-token-12345"
SLACK_SIGNING_SECRET = "test-signing-secret"
SLACK_SECRET = json.dumps({
    "botToken": SLACK_BOT_TOKEN,
    "signingSecret": SLACK_SIGNING_SECRET,
})
BOT_USER_ID = "UBOT123"
# Slack puts the app installation the event is for in every envelope; for a
# bot token its user_id is the bot user.
AUTHORIZATIONS = [{"enterprise_id": None, "team_id": "T12345TEAM", "user_id": BOT_USER_ID,
                   "is_bot": True, "is_enterprise_install": False}]
MESSAGE_TS = "1234567890.123456"


def _make_slack_message_event(text="Hello", user_id="U12345USER", channel_id="C12345CHAN",
                            subtype=None, bot_id=None, files=None, channel_type=None,
                            event_id="Ev12345MESSAGE", ts=MESSAGE_TS, thread_ts=None,
                            authorizations=AUTHORIZATIONS):
    """Build a Slack message event payload (a channel message unless channel_type says otherwise)."""
    event = {
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "user": user_id,
            "channel": channel_id,
            "text": text,
            "ts": ts,
        },
    }
    if authorizations is not None:
        event["authorizations"] = authorizations
    if channel_type:
        event["event"]["channel_type"] = channel_type
    if thread_ts:
        event["event"]["thread_ts"] = thread_ts
    if subtype:
        event["event"]["subtype"] = subtype
    if bot_id:
        event["event"]["bot_id"] = bot_id
    if files:
        event["event"]["files"] = files
    return event


def _make_slack_dm_event(**kwargs):
    """Build a Slack message event in the user's 1:1 DM with the bot."""
    kwargs.setdefault("channel_id", "D12345DM")
    return _make_slack_message_event(channel_type="im", **kwargs)


def _make_slack_app_mention_event(text="<@UBOT123> Hello", user_id="U12345USER",
                                 channel_id="C12345CHAN", event_id="Ev12345MENTION", ts=MESSAGE_TS,
                                 thread_ts=None):
    """Build a Slack app_mention event payload."""
    event = {
        "type": "event_callback",
        "event_id": event_id,
        "authorizations": AUTHORIZATIONS,
        "event": {
            "type": "app_mention",
            "user": user_id,
            "channel": channel_id,
            "text": text,
            "ts": ts,
        },
    }
    if thread_ts:
        event["event"]["thread_ts"] = thread_ts
    return event


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _dedupe_table():
    """An identity table whose put_item enforces attribute_not_exists(PK) for DEDUP# items."""
    table = MagicMock()
    table.get_item.return_value = {}
    claimed = set()

    def put_item(Item, ConditionExpression=None, **_):
        if Item["PK"].startswith("DEDUP#"):
            if ConditionExpression == "attribute_not_exists(PK)" and Item["PK"] in claimed:
                raise FakeClientError("ConditionalCheckFailedException")
            claimed.add(Item["PK"])

    table.put_item.side_effect = put_item
    return table


class _SlackTurnCase(unittest.TestCase):
    """Real dedupe against a fake identity table; the turn and the Slack sends are mocks."""

    def setUp(self):
        self.table = _dedupe_table()
        for name, value in (("identity_table", self.table), ("ClientError", FakeClientError)):
            patcher = patch.object(index, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, kwargs in (
            ("_get_slack_tokens", {"return_value": (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET)}),
            ("resolve_user", {"return_value": ("user_abc123", False)}),
            ("get_or_create_session", {"return_value": TEST_ROUTE}),
            ("authorize_channel_actor", {}),
            ("get_model_override", {"return_value": None}),
            ("invoke_agent_runtime", {"return_value": {"response": "agent reply"}}),
            ("send_slack_message", {}),
        ):
            patcher = patch.object(index, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)


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
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi"})
    @patch.object(index, "send_slack_message")
    def test_message_event_is_processed(self, mock_send, mock_invoke, mock_session, mock_resolve, mock_tokens):
        """Regular message events pass the type filter."""
        event = _make_slack_dm_event()
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        mock_invoke.assert_called_once()

    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
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
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
    @patch.object(index, "_download_slack_file", return_value=(b"img", "image/jpeg", None))
    @patch.object(index, "_upload_image_to_s3", return_value="user/img.jpg")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Nice image"})
    @patch.object(index, "send_slack_message")
    def test_file_share_subtype_is_allowed(self, mock_send, mock_invoke, mock_upload, mock_download, mock_session, mock_resolve, mock_tokens):
        """file_share subtype is allowed for image uploads."""
        event = _make_slack_dm_event(
            text="Check this image",
            subtype="file_share",
            files=[{"mimetype": "image/jpeg", "url_private_download": "https://example.com/img.jpg"}],
        )
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        mock_invoke.assert_called_once()


class TestSlackMentionStripping(unittest.TestCase):
    """Tests for bot mention stripping in Slack messages."""

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "The weather is sunny"})
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
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
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_dm_message_strips_mention_too(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Bot mention tags are also stripped from DM messages."""
        event = _make_slack_dm_event(text="<@UBOT123> Hello there")
        result = index.handle_slack(json.dumps(event))

        self.assertEqual(result["statusCode"], 200)
        invoke_args = mock_invoke.call_args
        message_sent = invoke_args[0][4]
        self.assertEqual(message_sent, "Hello there")

    @patch.object(index, "send_slack_message")
    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi"})
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
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
    @patch.object(index, "get_or_create_session", return_value=TEST_ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    @patch.object(index, "_get_slack_tokens", return_value=(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET))
    def test_message_without_mention_unchanged(self, mock_tokens, mock_resolve, mock_session, mock_invoke, mock_send):
        """Message without mention is not affected."""
        event = _make_slack_dm_event(text="Hello world")
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
        event = _make_slack_dm_event(bot_id="B12345BOT")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")


class TestSlackChannelMessages(_SlackTurnCase):
    """Every DM is answered; in a channel only messages addressed to the bot are, in their thread."""

    def test_dm_runs_a_turn_and_replies_top_level(self):
        index.handle_slack(json.dumps(_make_slack_dm_event(text="hello")))
        self.invoke_agent_runtime.assert_called_once()
        self.send_slack_message.assert_called_once_with("D12345DM", "agent reply", SLACK_BOT_TOKEN)

    def test_channel_message_not_addressed_to_the_bot_is_ignored(self):
        for event in (
            _make_slack_message_event(text="lunch at noon?", channel_type="channel"),
            _make_slack_message_event(text="ask <@UOTHER456> instead", channel_type="channel"),
            _make_slack_message_event(text="<@UBOT1234> is another bot", channel_type="channel"),
            _make_slack_message_event(text="private channel chat", channel_id="G12345PRIV", channel_type="group"),
            _make_slack_message_event(text="group DM chat", channel_id="G12345MPIM", channel_type="mpim"),
            # No channel_type: a C... channel is not a DM.
            _make_slack_message_event(text="lunch at noon?"),
            # Without the envelope's authorizations the bot user is unknown.
            _make_slack_message_event(text="<@UBOT123> hi", channel_type="channel", authorizations=None),
        ):
            with self.subTest(text=event["event"]["text"], channel_type=event["event"].get("channel_type")):
                self.assertEqual(index.handle_slack(json.dumps(event)), {"statusCode": 200, "body": "ok"})
        self.resolve_user.assert_not_called()
        self.invoke_agent_runtime.assert_not_called()
        self.send_slack_message.assert_not_called()
        self.table.put_item.assert_not_called()

    def test_open_registration_enrolls_no_one_from_unaddressed_channel_messages(self):
        with patch.object(index, "resolve_user", REAL_RESOLVE_USER), \
                patch.object(index, "REGISTRATION_OPEN", True), \
                patch.object(index, "RUNTIME_TARGET", {"runtimeArn": "arn:test"}), \
                patch.object(index, "enroll_new_user", return_value=("user_new", True)) as enroll:
            index.handle_slack(json.dumps(_make_slack_message_event(text="lunch at noon?", channel_type="channel")))
            enroll.assert_not_called()
            self.send_slack_message.assert_not_called()
            # Control: the same sender in a DM is enrolled, so the check above can fail.
            index.handle_slack(json.dumps(_make_slack_dm_event(text="lunch at noon?")))
            enroll.assert_called_once()
        self.invoke_agent_runtime.assert_called_once()

    def test_channel_message_that_mentions_the_bot_runs_once_in_its_thread(self):
        event = _make_slack_message_event(text="<@UBOT123> summarize this", channel_type="channel")
        index.handle_slack(json.dumps(event))
        self.invoke_agent_runtime.assert_called_once()
        self.assertEqual(self.invoke_agent_runtime.call_args[0][4], "summarize this")
        self.send_slack_message.assert_called_once_with(
            "C12345CHAN", "agent reply", SLACK_BOT_TOKEN, thread_ts=MESSAGE_TS)

    def test_channel_turns_are_marked_shared_and_dm_turns_are_not(self):
        """The bridge adds the shared-conversation rules to the prompt for shared_chat turns."""
        cases = (
            (_make_slack_dm_event(text="hello"), False),
            (_make_slack_message_event(text="<@UBOT123> hi", channel_type="channel", event_id="Ev1"), True),
            (_make_slack_app_mention_event(text="<@UBOT123> hi", event_id="Ev2", ts="1234567890.200000"), True),
            (_make_slack_message_event(text="<@UBOT123> hi", channel_id="G12345PRIV", channel_type="group",
                                       event_id="Ev3"), True),
            (_make_slack_message_event(text="<@UBOT123> hi", channel_id="G12345MPIM", channel_type="mpim",
                                       event_id="Ev4"), True),
        )
        for event, shared in cases:
            with self.subTest(channel=event["event"]["channel"], event_type=event["event"]["type"]):
                self.invoke_agent_runtime.reset_mock()
                index.handle_slack(json.dumps(event))
                self.invoke_agent_runtime.assert_called_once()
                self.assertIs(self.invoke_agent_runtime.call_args.kwargs["shared_chat"], shared)

    def test_mention_inside_a_thread_is_answered_in_that_thread(self):
        for event in (
            _make_slack_message_event(text="<@UBOT123> and this?", channel_type="channel",
                                      ts="1234567890.200000", thread_ts=MESSAGE_TS),
            _make_slack_app_mention_event(text="<@UBOT123> and that?", event_id="Ev12345MENTION2",
                                          ts="1234567890.300000", thread_ts=MESSAGE_TS),
        ):
            with self.subTest(event_type=event["event"]["type"]):
                self.send_slack_message.reset_mock()
                index.handle_slack(json.dumps(event))
                self.send_slack_message.assert_called_once_with(
                    "C12345CHAN", "agent reply", SLACK_BOT_TOKEN, thread_ts=MESSAGE_TS)

    def test_app_mention_and_message_events_for_one_post_run_one_turn(self):
        mention = _make_slack_app_mention_event(text="<@UBOT123> summarize this")
        message = _make_slack_message_event(text="<@UBOT123> summarize this", channel_type="channel")
        for first, second in ((mention, message), (message, mention)):
            with self.subTest(first=first["event"]["type"]), patch.object(index, "identity_table", _dedupe_table()):
                self.invoke_agent_runtime.reset_mock()
                self.send_slack_message.reset_mock()
                index.handle_slack(json.dumps(first))
                index.handle_slack(json.dumps(second))
                self.invoke_agent_runtime.assert_called_once()
                self.send_slack_message.assert_called_once()

    def test_bot_is_private_notice_is_never_posted_in_a_channel(self):
        self.resolve_user.return_value = (None, False)
        for event in (
            _make_slack_app_mention_event(text="<@UBOT123> hi"),
            _make_slack_message_event(text="<@UBOT123> hi", channel_type="channel",
                                      event_id="Ev12345OTHER", ts="1234567890.400000"),
            _make_slack_message_event(text="hi everyone", channel_type="channel",
                                      event_id="Ev12345CHATTER", ts="1234567890.500000"),
        ):
            with self.subTest(text=event["event"]["text"], event_type=event["event"]["type"]):
                index.handle_slack(json.dumps(event))
        self.assertEqual(self.resolve_user.call_count, 2)  # the two addressed messages were refused
        self.send_slack_message.assert_not_called()
        self.invoke_agent_runtime.assert_not_called()
        # A DM still gets the notice with the sender's ID.
        index.handle_slack(json.dumps(_make_slack_dm_event(text="hi")))
        self.send_slack_message.assert_called_once()
        self.assertEqual(self.send_slack_message.call_args[0][0], "D12345DM")
        self.assertIn("Your ID: `slack:U12345USER`", self.send_slack_message.call_args[0][1])

    def test_command_replies_in_a_channel_go_to_the_thread(self):
        with patch.object(index, "handle_model_command", return_value="Current model: default"):
            index.handle_slack(json.dumps(_make_slack_app_mention_event(text="<@UBOT123> /model")))
        index.handle_slack(json.dumps(_make_slack_message_event(
            text="<@UBOT123> link", channel_type="channel", event_id="Ev12345LINK", ts="1234567890.600000")))
        self.assertEqual([c.kwargs for c in self.send_slack_message.call_args_list],
                         [{"thread_ts": MESSAGE_TS}, {"thread_ts": "1234567890.600000"}])
        self.assertEqual(self.send_slack_message.call_args[0][1], index.LINK_DM_ONLY_MESSAGE)
        self.invoke_agent_runtime.assert_not_called()

    def test_bot_user_id_comes_from_the_envelope_authorizations(self):
        self.assertEqual(index._slack_bot_user_id({"authorizations": AUTHORIZATIONS}), BOT_USER_ID)
        self.assertEqual(index._slack_bot_user_id({"authorizations": [
            {"user_id": "UHUMAN1", "is_bot": False}, {"user_id": "UBOT9", "is_bot": True}]}), "UBOT9")
        self.assertEqual(index._slack_bot_user_id({"authorizations": [{"user_id": "UFIRST1"}]}), "UFIRST1")
        for envelope in ({}, {"authorizations": []}, {"authorizations": "UBOT123"}, {"authorizations": 5},
                         {"authorizations": [{}]}, {"authorizations": ["UBOT123"]}):
            with self.subTest(envelope=envelope):
                self.assertEqual(index._slack_bot_user_id(envelope), "")

    def test_mention_forms(self):
        envelope = {"authorizations": AUTHORIZATIONS}
        for text, addressed in (("<@UBOT123> hi", True), ("hi <@UBOT123>", True),
                                ("<@UBOT123|openclaw> hi", True), ("<@UBOT1234> hi", False),
                                ("@UBOT123 hi", False), ("UBOT123", False)):
            with self.subTest(text=text):
                event = {"type": "message", "channel_type": "channel", "text": text}
                self.assertEqual(index._slack_addresses_bot(envelope, event), addressed)
        self.assertTrue(index._slack_addresses_bot({}, {"type": "app_mention", "text": "hi"}))
        self.assertFalse(index._slack_addresses_bot(envelope, {"type": "message", "text": None}))


class TestSlackRetryHandling(_SlackTurnCase):
    """Slack retries are deduplicated by event_id and by (channel, ts), not dropped by header."""

    RETRY = {"x-slack-retry-num": "1", "x-slack-retry-reason": "http_timeout"}

    def test_retry_of_a_handled_delivery_runs_no_second_turn(self):
        body = json.dumps(_make_slack_dm_event())
        index.handle_slack(body)
        result = index.handle_slack(body, headers=self.RETRY)
        self.assertEqual(result, {"statusCode": 200, "body": "ok"})
        self.invoke_agent_runtime.assert_called_once()
        self.send_slack_message.assert_called_once()

    def test_replayed_event_id_runs_no_second_turn(self):
        index.handle_slack(json.dumps(_make_slack_dm_event(event_id="Ev12345REPLAY")))
        index.handle_slack(json.dumps(_make_slack_dm_event(event_id="Ev12345REPLAY", ts="1234567890.999999")))
        self.invoke_agent_runtime.assert_called_once()

    def test_retry_whose_first_delivery_was_lost_runs_the_turn(self):
        index.handle_slack(json.dumps(_make_slack_dm_event()), headers=self.RETRY)
        self.invoke_agent_runtime.assert_called_once()
        self.send_slack_message.assert_called_once()

    def test_claim_happens_before_any_reply(self):
        self.resolve_user.return_value = (None, False)
        body = json.dumps(_make_slack_dm_event())
        index.handle_slack(body)
        index.handle_slack(body, headers=self.RETRY)
        self.send_slack_message.assert_called_once()  # one "bot is private" notice, not two
        self.assertEqual(self.resolve_user.call_count, 1)

    def test_dedupe_keys(self):
        before = int(time.time())
        self.assertTrue(index._claim_slack_event("Ev12345KEYS", "C12345CHAN", MESSAGE_TS))
        items = [c.kwargs for c in self.table.put_item.call_args_list]
        self.assertEqual([i["Item"]["PK"] for i in items],
                         ["DEDUP#slack:event:Ev12345KEYS", f"DEDUP#slack:message:C12345CHAN:{MESSAGE_TS}"])
        for item in items:
            self.assertEqual(item["Item"]["SK"], "DEDUP")
            self.assertEqual(item["ConditionExpression"], "attribute_not_exists(PK)")
            self.assertGreaterEqual(item["Item"]["ttl"], before + index.SLACK_DEDUP_TTL_SECONDS)
        self.assertFalse(index._claim_slack_event("Ev12345KEYS", "C12345CHAN", "1234567890.000001"))
        self.assertFalse(index._claim_slack_event("Ev12345OTHER", "C12345CHAN", MESSAGE_TS))

    def test_dedupe_write_error_still_answers(self):
        """A DynamoDB error other than the failed condition proceeds, as for Feishu."""
        self.table.put_item.side_effect = FakeClientError("ProvisionedThroughputExceededException")
        index.handle_slack(json.dumps(_make_slack_dm_event()))
        self.invoke_agent_runtime.assert_called_once()

    def test_webhook_dispatches_retries(self):
        request = {"requestContext": {"http": {"method": "POST", "path": "/webhook/slack", "sourceIp": "192.0.2.1"}},
                   "headers": {"x-slack-retry-num": "2", "x-slack-retry-reason": "http_timeout"},
                   "body": json.dumps(_make_slack_dm_event())}
        with patch.object(index, "validate_slack_webhook", return_value=True), \
                patch.object(index, "_self_invoke_async") as dispatch:
            self.assertEqual(index.handler(request, None), {"statusCode": 200, "body": "ok"})
        dispatch.assert_called_once_with("slack", request["body"], request["headers"])


class TestSlackSend(unittest.TestCase):
    """chat.postMessage gets mrkdwn, thread_ts only when given, and ok:false is logged by code."""

    def _payloads(self, *calls, response=b'{"ok": true}'):
        with patch.object(index.urllib_request, "urlopen") as urlopen:
            urlopen.return_value.read.return_value = response
            for call in calls:
                call()
        return [json.loads(c.args[0].data) for c in urlopen.call_args_list]

    def test_text_is_sent_as_mrkdwn_with_model_mentions_escaped(self):
        payload, = self._payloads(lambda: index.send_slack_message(
            "D12345DM", "## Brief\n**Rain** <!channel> [map](https://example.com/m)", SLACK_BOT_TOKEN))
        self.assertEqual(payload["text"], "*Brief*\n*Rain* &lt;!channel&gt; <https://example.com/m|map>")

    def test_accepted_message_returns_true_without_logging(self):
        with self.assertNoLogs(index.logger, level="WARNING"):
            results = []
            self._payloads(lambda: results.append(index.send_slack_message("D12345DM", "hi", SLACK_BOT_TOKEN)))
        self.assertEqual(results, [True])

    def test_ok_false_is_logged_with_slack_error_code_only(self):
        response = b'{"ok": false, "error": "not_in_channel", "warning": "detail-from-body"}'
        results = []
        with self.assertLogs(index.logger, level="ERROR") as logs:
            self._payloads(lambda: results.append(
                index.send_slack_message("C12345CHAN", "secret brief", SLACK_BOT_TOKEN)), response=response)
        self.assertEqual(results, [False])
        output = "\n".join(logs.output)
        self.assertIn("error=not_in_channel", output)
        for leaked in ("detail-from-body", "secret brief", SLACK_BOT_TOKEN):
            self.assertNotIn(leaked, output)

    def test_unreadable_response_and_network_error_return_false(self):
        results = []
        with self.assertLogs(index.logger, level="ERROR") as logs:
            self._payloads(lambda: results.append(index.send_slack_message("D12345DM", "hi", SLACK_BOT_TOKEN)),
                           response=b"<html>502 Bad Gateway</html>")
        self.assertIn("error=unreadable_response", "\n".join(logs.output))
        with patch.object(index.urllib_request, "urlopen", side_effect=TimeoutError("timed out")), \
                self.assertLogs(index.logger, level="ERROR"):
            results.append(index.send_slack_message("D12345DM", "hi", SLACK_BOT_TOKEN))
        self.assertEqual(results, [False, False])

    def test_thread_ts_is_sent_only_when_given(self):
        top_level, threaded = self._payloads(
            lambda: index.send_slack_message("D12345DM", "hi", SLACK_BOT_TOKEN),
            lambda: index.send_slack_message("C12345CHAN", "hi", SLACK_BOT_TOKEN, thread_ts=MESSAGE_TS),
        )
        self.assertEqual(top_level, {"channel": "D12345DM", "text": "hi"})
        self.assertEqual(threaded, {"channel": "C12345CHAN", "text": "hi", "thread_ts": MESSAGE_TS})

    def test_progress_notice_follows_the_thread(self):
        with patch.object(index, "send_slack_message") as send:
            index._slack_progress_notify("C12345CHAN", SLACK_BOT_TOKEN, threading.Event(), notify_after_s=0,
                                         thread_ts=MESSAGE_TS)
            index._slack_progress_notify("D12345DM", SLACK_BOT_TOKEN, threading.Event(), notify_after_s=0)
        self.assertEqual(send.call_args_list[0].kwargs, {"thread_ts": MESSAGE_TS})
        self.assertEqual(send.call_args_list[1].kwargs, {})


class TestSlackEdgeCases(unittest.TestCase):
    """Tests for edge cases in Slack handling."""

    def test_empty_user_id_ignored(self):
        """Events without user ID are ignored."""
        event = _make_slack_dm_event(user_id="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    def test_empty_channel_id_ignored(self):
        """Events without channel ID are ignored."""
        event = _make_slack_dm_event(channel_id="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    def test_empty_text_without_image_ignored(self):
        """Events without text and without image are ignored."""
        event = _make_slack_dm_event(text="")
        result = index.handle_slack(json.dumps(event))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")


if __name__ == "__main__":
    unittest.main()
