"""Tests for Telegram reply chunking in the Router Lambda.

Telegram's sendMessage limit (4096) is counted in UTF-16 code units, so an
emoji or other astral character counts 2. The router split long replies by
Python string length, which produced chunks over the limit; Telegram
rejected them (HTML and plain-text retry alike) and the chunk was dropped.
"""

import html
import json
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

index = importlib.import_module("index")

TELEGRAM_LIMIT = 4096


def utf16_units(s):
    return len(s.encode("utf-16-le")) // 2


def telegram_visible_text(payload):
    """Text length as Telegram measures it: tags removed, entities decoded."""
    text = payload["text"]
    if payload.get("parse_mode") == "HTML":
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text


def emoji_reply(code_points=6000):
    line = "Market update 📈 stocks up 🚀, bonds flat 😐, crypto 🔥🔥\n"
    text = ""
    while len(text) < code_points:
        text += line
    return text[:code_points]


def cjk_reply():
    return "今天的市场简报：股票上涨，债券持平。\n" * 450  # 8550 units


def fenced_reply():
    prose = "intro line\n" * 250  # 2750 units
    fence = "```\n" + "code line\n" * 150 + "```\n"  # ~1508 units
    return prose + fence + "outro line\n" * 250


class FakeTelegram:
    """urlopen stub that enforces Telegram's UTF-16 length limit."""

    def __init__(self, fail_always=False):
        self.accepted = []
        self.rejected = []
        self.fail_always = fail_always

    def __call__(self, req, timeout=None):
        payload = json.loads(req.data.decode())
        visible = telegram_visible_text(payload)
        if self.fail_always or utf16_units(visible) > TELEGRAM_LIMIT:
            self.rejected.append(payload)
            raise Exception("HTTP Error 400: Bad Request: message is too long")
        self.accepted.append(payload)
        return MagicMock()


def telegram_update(text="hello"):
    return {
        "message": {
            "text": text,
            "chat": {"id": 123456},
            "from": {"id": 123456, "first_name": "Test"},
        }
    }


def handle_reply(fake, reply_text):
    """Run handle_telegram end to end with the agent returning reply_text."""
    with patch.object(index, "_get_telegram_token", return_value="tok"), \
            patch.object(index, "resolve_user", return_value=("user_test", False)), \
            patch.object(index, "get_or_create_session", return_value="ses_test"), \
            patch.object(index, "send_telegram_typing"), \
            patch.object(index, "_periodic_typing"), \
            patch.object(index, "invoke_agent_runtime", return_value={"response": reply_text}), \
            patch.object(index.urllib_request, "urlopen", side_effect=fake):
        index.handle_telegram(telegram_update())


class TestHandleTelegramLongReply(unittest.TestCase):

    def assert_delivered(self, text):
        fake = FakeTelegram()
        handle_reply(fake, text)
        for p in fake.accepted:
            self.assertLessEqual(utf16_units(telegram_visible_text(p)), TELEGRAM_LIMIT)
        # Every chunk was accepted as HTML on the first try; nothing dropped.
        self.assertEqual(fake.rejected, [])
        self.assertEqual("".join(telegram_visible_text(p) for p in fake.accepted), text)
        return fake

    def test_emoji_reply_no_chunk_dropped(self):
        fake = self.assert_delivered(emoji_reply())
        self.assertGreater(len(fake.accepted), 1)

    def test_short_emoji_reply_over_4096_units_is_split(self):
        # 3000 code points but 6000 UTF-16 units: the old "<= 4096" check sent it whole.
        self.assert_delivered("🎉" * 3000)

    def test_cjk_reply(self):
        self.assert_delivered(cjk_reply())

    def test_fenced_code_reply(self):
        # The HTML conversion turns ``` fences into <pre>, so compare the
        # Markdown chunks handed to send_telegram_message: each must keep its
        # fences balanced (a split inside a fence breaks the rendering).
        text = fenced_reply()
        sent = []
        with patch.object(index, "send_telegram_message",
                          side_effect=lambda c, t, tok: sent.append(t) or True):
            handle_reply(FakeTelegram(), text)
        self.assertGreater(len(sent), 1)
        self.assertEqual("".join(sent), text)
        for chunk in sent:
            self.assertLessEqual(utf16_units(chunk), TELEGRAM_LIMIT)
            self.assertEqual(chunk.count("```") % 2, 0, chunk[-200:])
        self.assert_delivered(text.replace("```", ""))

    def test_short_reply_sent_once_as_html(self):
        fake = FakeTelegram()
        handle_reply(fake, "Good morning ☀️ **here** is your reply")
        self.assertEqual(len(fake.accepted), 1)
        self.assertEqual(fake.accepted[0].get("parse_mode"), "HTML")

    def test_streamed_reply_not_resent(self):
        fake = FakeTelegram()
        with patch.object(index, "_get_telegram_token", return_value="tok"), \
                patch.object(index, "resolve_user", return_value=("user_test", False)), \
                patch.object(index, "get_or_create_session", return_value="ses_test"), \
                patch.object(index, "send_telegram_typing"), \
                patch.object(index, "_periodic_typing"), \
                patch.object(index, "invoke_agent_runtime",
                             return_value={"response": emoji_reply(), "streamed": True}), \
                patch.object(index.urllib_request, "urlopen", side_effect=fake):
            index.handle_telegram(telegram_update())
        self.assertEqual(fake.accepted, [])

    def test_failed_chunk_is_logged_as_dropped(self):
        fake = FakeTelegram(fail_always=True)
        with self.assertLogs(index.logger, level="ERROR") as logs:
            handle_reply(fake, emoji_reply())
        self.assertTrue(any("dropped" in m.lower() for m in logs.output), logs.output)


class TestSendTelegramChunks(unittest.TestCase):

    def test_failed_chunk_retried_smaller(self):
        calls = []

        def send(chat_id, text, token):
            calls.append(text)
            return utf16_units(text) <= 2000

        text = "word " * 1000  # 5000 units
        with patch.object(index, "send_telegram_message", side_effect=send):
            ok = index._send_telegram_chunks("123456", text, "tok")
        self.assertTrue(ok)
        delivered = "".join(t for t in calls if utf16_units(t) <= 2000)
        self.assertEqual(delivered, text)

    def test_send_telegram_message_reports_result(self):
        with patch.object(index.urllib_request, "urlopen", side_effect=FakeTelegram()):
            self.assertTrue(index.send_telegram_message("123456", "hi", "tok"))
        with patch.object(index.urllib_request, "urlopen", side_effect=FakeTelegram(fail_always=True)):
            self.assertFalse(index.send_telegram_message("123456", "hi", "tok"))


class TestSplitTelegramText(unittest.TestCase):

    def assert_valid_split(self, text, chunks, limit):
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            self.assertTrue(c)
            self.assertLessEqual(utf16_units(c), limit)
            c.encode("utf-16-le", errors="strict")  # no lone surrogate

    def test_emoji_only_no_separators(self):
        text = "😀" * 5000
        self.assert_valid_split(text, index._split_telegram_text(text, limit=4000), 4000)

    def test_astral_cjk(self):
        text = "𠀀𠀁𠀂" * 2000  # CJK Extension B, 2 units each
        self.assert_valid_split(text, index._split_telegram_text(text, limit=4000), 4000)

    def test_does_not_split_inside_code_fence(self):
        text = fenced_reply()
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, c[-200:])

    def test_short_and_empty(self):
        self.assertEqual(index._split_telegram_text("hi 👋"), ["hi 👋"])
        self.assertEqual(index._split_telegram_text(""), [])


if __name__ == "__main__":
    unittest.main()
