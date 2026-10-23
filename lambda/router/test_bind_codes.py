"""Tests for cross-channel bind codes in the Router Lambda.

Covers the code format and parser, and that a code is only ever sent into a
1:1 conversation with the requesting user.

All IDs, tokens and secrets below are synthetic placeholders.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
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

ROUTE = SimpleNamespace(actor_id="telegram:100000001", generation=1, session_id="ses_test")
CODE = "ABCDEFGHJKMNPQRS"


class TestCodeFormat(unittest.TestCase):
    def test_codes_are_16_base32_chars(self):
        codes = {index._generate_bind_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for code in codes:
            self.assertEqual(len(code), 16)
            self.assertTrue(set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"), code)

    def test_codes_come_from_the_os_csprng(self):
        with patch.object(index.secrets, "token_bytes", return_value=b"\x00" * 10) as rng:
            self.assertEqual(index._generate_bind_code(), "A" * 16)
        rng.assert_called_once_with(10)

    def test_create_stores_the_generated_code(self):
        table = MagicMock()
        with patch.object(index, "_binding_route", return_value=ROUTE), \
                patch.object(index, "identity_table", table), \
                patch.object(index, "_generate_bind_code", return_value=CODE):
            self.assertEqual(index.create_bind_code("user_abc"), CODE)
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["PK"], f"BIND#{CODE}")
        self.assertEqual(item["userId"], "user_abc")

    def test_parser_accepts_new_and_pre_upgrade_codes(self):
        self.assertEqual(index._is_bind_command(f"link {CODE.lower()}"), (True, CODE))
        self.assertEqual(index._is_bind_command("bind A1B2C3D4"), (True, "A1B2C3D4"))

    def test_parser_rejects_other_shapes(self):
        for text in ("link", "link ABC", "link " + "A" * 33, "link ABCD-1234", "link ＡＢＣＤＥＦＧＨ",
                     "link ABCDEFGH extra", "hello ABCDEFGH"):
            with self.subTest(text=text):
                self.assertEqual(index._is_bind_command(text), (False, ""))


class _LinkCommandBase(unittest.TestCase):
    def setUp(self):
        for name, value in (("resolve_user", ("user_abc", False)), ("get_or_create_session", ROUTE),
                            ("authorize_channel_actor", None), ("create_bind_code", CODE)):
            p = patch.object(index, name, return_value=value)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)


class TestTelegramLink(_LinkCommandBase):
    def _send(self, chat):
        update = {"message": {"text": "link", "chat": chat, "from": {"id": 100000001, "first_name": "T"}}}
        with patch.object(index, "send_telegram_message") as send, \
                patch.object(index, "_get_telegram_token", return_value="tg-token"):
            index.handle_telegram(json.dumps(update))
        return send

    def test_private_chat_gets_the_code(self):
        send = self._send({"id": 100000001, "type": "private"})
        self.create_bind_code.assert_called_once_with("user_abc")
        self.assertIn(CODE, send.call_args[0][1])

    def test_group_chats_get_no_code(self):
        # Groups are ignored outright while telegram_groups_enabled is off, and a
        # group never reaches the sender's link flow when it is on.
        for groups_enabled in (False, True):
            for chat in ({"id": -100000002, "type": "group"}, {"id": -100000003, "type": "supergroup"}):
                with self.subTest(chat=chat, groups_enabled=groups_enabled), \
                        patch.object(index, "TELEGRAM_GROUPS_ENABLED", groups_enabled), \
                        patch.object(index, "_telegram_bot_username", return_value="TestBot"):
                    self.create_bind_code.reset_mock()
                    send = self._send(chat)
                    self.create_bind_code.assert_not_called()
                    self.assertFalse(any(CODE in str(c) for c in send.call_args_list))
        for chat in ({"id": 100000009, "type": "private"}, {"id": 100000001}):
            with self.subTest(chat=chat):
                self.create_bind_code.reset_mock()
                send = self._send(chat)
                self.create_bind_code.assert_not_called()
                self.assertEqual(send.call_args[0][1], index.LINK_DM_ONLY_MESSAGE)
                self.assertNotIn(CODE, send.call_args[0][1])


class TestSlackLink(_LinkCommandBase):
    def _send(self, **event):
        body = {"event": {"type": "message", "text": "link", "user": "U0XXXXXX1", "channel": "D0XXXXXX1", **event}}
        with patch.object(index, "send_slack_message") as send, \
                patch.object(index, "_get_slack_tokens", return_value=("xoxb-test", "signing")):
            index.handle_slack(json.dumps(body))
        return send

    def test_dm_gets_the_code(self):
        for event in ({"channel_type": "im"}, {}):
            with self.subTest(event=event):
                self.create_bind_code.reset_mock()
                send = self._send(**event)
                self.create_bind_code.assert_called_once_with("user_abc")
                self.assertIn(CODE, send.call_args[0][1])

    def test_channels_and_mentions_get_no_code(self):
        for event in ({"type": "app_mention", "channel": "C0XXXXXX1", "text": "<@U0BOT0001> link"},
                      {"channel": "C0XXXXXX1", "channel_type": "channel"},
                      {"channel": "G0XXXXXX1", "channel_type": "group"},
                      {"channel": "G0XXXXXX2", "channel_type": "mpim"},
                      {"channel": "D0XXXXXX1", "channel_type": "mpim"}):
            with self.subTest(event=event):
                self.create_bind_code.reset_mock()
                send = self._send(**event)
                self.create_bind_code.assert_not_called()
                self.assertEqual(send.call_args[0][1], index.LINK_DM_ONLY_MESSAGE)


class TestFeishuLink(_LinkCommandBase):
    def _send(self, message_extra):
        message = {"chat_id": "oc_chat1", "message_type": "text", "content": json.dumps({"text": "link"}),
                   **message_extra}
        event = {"header": {"event_type": "im.message.receive_v1"},
                 "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_test1"}},
                           "message": message}}
        with patch.object(index, "send_feishu_message") as send:
            index.handle_feishu(json.dumps(event))
        return send

    def test_p2p_chat_gets_the_code(self):
        send = self._send({"chat_type": "p2p"})
        self.create_bind_code.assert_called_once_with("user_abc")
        self.assertIn(CODE, send.call_args[0][1])

    def test_group_or_unknown_chat_gets_no_code(self):
        for extra in ({"chat_type": "group"}, {}):
            with self.subTest(extra=extra):
                self.create_bind_code.reset_mock()
                send = self._send(extra)
                self.create_bind_code.assert_not_called()
                self.assertNotIn(CODE, send.call_args[0][1])


class TestOneToOneChannelsUnchanged(_LinkCommandBase):
    """WhatsApp and LINE only deliver to the sender; Discord replies ephemerally in guilds."""

    def test_whatsapp_sends_code_to_sender(self):
        with patch.object(index.whatsapp, "send_text") as send:
            index._handle_whatsapp_message({}, {"from": "15550000001", "text": "link", "image_id": None})
        self.assertEqual(send.call_args[0][1], "15550000001")
        self.assertIn(CODE, send.call_args[0][2])

    def test_line_sends_code_to_sender(self):
        with patch.object(index.line_channel, "send_text") as send:
            index._handle_line_event({}, {"user_id": "U" + "0" * 32, "text": "link", "message_id": None,
                                          "reply_token": "rt"})
        self.assertIn(CODE, send.call_args[0][2])

    def test_discord_reply_carries_code(self):
        reply = MagicMock()
        index._handle_discord_command({"user_id": "100000000000000001", "text": "link", "attachment": None,
                                       "name": "d"}, reply)
        self.assertIn(CODE, reply.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
