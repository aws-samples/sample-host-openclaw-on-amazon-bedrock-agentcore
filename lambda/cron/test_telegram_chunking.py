"""Tests for Telegram delivery chunking in the Cron Lambda.

Telegram's sendMessage limit (4096) is counted in UTF-16 code units, so an
emoji or other astral character counts 2. Splitting by Python string length
produced chunks over the limit that Telegram rejected, and the chunk was
dropped.
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


def emoji_brief(code_points=6000):
    line = "Market update 📈 stocks up 🚀, bonds flat 😐, crypto 🔥🔥\n"
    text = ""
    while len(text) < code_points:
        text += line
    return text[:code_points]


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


def deliver_with(fake, text):
    with patch.object(index, "_get_telegram_token", return_value="tok"), \
            patch.object(index.urllib_request, "urlopen", side_effect=fake):
        index.deliver_response("telegram", "12345", text)


class TestDeliverResponseTelegram(unittest.TestCase):

    def test_emoji_brief_chunks_within_utf16_limit(self):
        sent = []
        with patch.object(index, "_get_telegram_token", return_value="tok"), \
                patch.object(index, "send_telegram_message",
                             side_effect=lambda c, t, tok: sent.append(t) or True):
            index.deliver_response("telegram", "12345", emoji_brief())
        self.assertGreater(len(sent), 1)
        for chunk in sent:
            self.assertLessEqual(utf16_units(chunk), TELEGRAM_LIMIT)
        self.assertEqual("".join(sent), emoji_brief())

    def test_emoji_brief_no_chunk_dropped(self):
        fake = FakeTelegram()
        text = emoji_brief()
        deliver_with(fake, text)
        delivered = "".join(telegram_visible_text(p) for p in fake.accepted)
        # Every chunk was delivered, via HTML; nothing had to fall back or drop.
        self.assertEqual(fake.rejected, [])
        self.assertEqual(delivered, text)

    def test_short_emoji_message_over_4096_units_is_split(self):
        # 3000 code points but 6000 UTF-16 units: the old "<= 4096" check sent it whole.
        text = "🎉" * 3000
        fake = FakeTelegram()
        deliver_with(fake, text)
        self.assertEqual(fake.rejected, [])
        self.assertEqual("".join(telegram_visible_text(p) for p in fake.accepted), text)

    def test_short_message_sent_once(self):
        fake = FakeTelegram()
        deliver_with(fake, "Good morning ☀️ here is your brief")
        self.assertEqual(len(fake.accepted), 1)

    def test_failed_chunk_is_logged_not_silently_dropped(self):
        fake = FakeTelegram(fail_always=True)
        with self.assertLogs(index.logger, level="ERROR") as logs:
            deliver_with(fake, emoji_brief())
        self.assertTrue(any("dropped" in m.lower() for m in logs.output), logs.output)

    def test_failed_chunk_retried_smaller(self):
        # A chunk that Telegram rejects is re-sent in smaller pieces.
        calls = []

        def send(chat_id, text, token):
            calls.append(text)
            return utf16_units(text) <= 2000

        text = "word " * 1000  # 5000 units
        with patch.object(index, "_get_telegram_token", return_value="tok"), \
                patch.object(index, "send_telegram_message", side_effect=send):
            ok = index._send_telegram_chunks("12345", text, "tok")
        self.assertTrue(ok)
        delivered = "".join(t for t in calls if utf16_units(t) <= 2000)
        self.assertEqual(delivered, text)


class TestSplitTelegramText(unittest.TestCase):

    def assert_valid_split(self, text, chunks, limit):
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            self.assertTrue(c)
            self.assertLessEqual(utf16_units(c), limit)
            # No lone surrogate: each chunk round-trips through UTF-16.
            c.encode("utf-16-le", errors="strict")

    def test_emoji_heavy(self):
        text = emoji_brief(9000)
        chunks = index._split_telegram_text(text)
        self.assert_valid_split(text, chunks, TELEGRAM_LIMIT)

    def test_emoji_only_no_separators(self):
        text = "😀" * 5000
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)

    def test_cjk(self):
        text = "今天的市场简报：股票上涨，债券持平。" * 400
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)

    def test_astral_cjk(self):
        text = "𠀀𠀁𠀂" * 2000  # CJK Extension B, 2 units each
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)

    def test_prefers_paragraph_boundary(self):
        para = ("x" * 99 + "\n") * 30  # 3000 units
        text = para + "\n" + para
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)
        self.assertEqual(chunks[0], para + "\n")

    def test_does_not_split_inside_code_fence(self):
        prose = "intro line\n" * 250  # 2750 units
        fence = "```\n" + "code line\n" * 150 + "```\n"  # ~1508 units
        text = prose + fence + "outro\n" * 100
        chunks = index._split_telegram_text(text, limit=4000)
        self.assert_valid_split(text, chunks, 4000)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, c[-200:])

    def test_short_text_single_chunk(self):
        self.assertEqual(index._split_telegram_text("hi 👋"), ["hi 👋"])

    def test_empty(self):
        self.assertEqual(index._split_telegram_text(""), [])


if __name__ == "__main__":
    unittest.main()
