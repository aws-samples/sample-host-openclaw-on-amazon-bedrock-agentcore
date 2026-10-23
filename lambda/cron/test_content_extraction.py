"""Tests for _extract_text_from_content_blocks in the Cron Lambda.

The cron Lambda keeps its own copy of the router's content-block extractor.
These cases mirror the #118 fixes in lambda/router/test_content_extraction.py
so a scheduled brief cannot leak raw '[{"type":"text",...' JSON into a
Telegram/Slack/Feishu message.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

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


class TestCronExtractTextFromContentBlocks(unittest.TestCase):
    """Content-block extraction in the cron Lambda matches the router (#118)."""

    def test_malformed_block_mid_reply_keeps_suffix(self):
        """A closed but malformed block (trailing comma) must not drop the text after it."""
        result = index._extract_text_from_content_blocks(
            'prefix [{"type":"text","text":"x"},] suffix'
        )
        self.assertNotIn("[{", result)
        self.assertTrue(result.startswith("prefix "), result)
        self.assertTrue(result.endswith(" suffix"), result)

    def test_trailing_partial_fragment_after_text_is_stripped(self):
        """A genuine unterminated fragment at the end is stripped; prefix kept."""
        result = index._extract_text_from_content_blocks(
            'Here is the answer.\n\n[{"type":"text","text":"partial'
        )
        self.assertEqual(result, "Here is the answer.\n\n")

    def test_prose_mentioning_block_syntax_is_unchanged(self):
        """Prose that quotes content-block syntax mid-reply keeps all its text."""
        raw = (
            'To send an image, the API expects content like '
            '[{"type": "image", ...}]. After that, call the second endpoint '
            'with the returned id.'
        )
        self.assertEqual(index._extract_text_from_content_blocks(raw), raw)

    def test_well_formed_blocks_still_unwrapped(self):
        """Regression guard: nested well-formed blocks are still fully unwrapped."""
        inner = json.dumps([{"type": "text", "text": "Daily brief."}])
        outer = json.dumps([{"type": "text", "text": inner}])
        self.assertEqual(index._extract_text_from_content_blocks(outer), "Daily brief.")

    def test_partial_content_block_json(self):
        """A reply that is only a partial content block does not leak '[{'."""
        result = index._extract_text_from_content_blocks('\n\n[{"type":"text","text":"hello')
        self.assertNotIn("[{", result)

    def test_deeply_nested_json_is_kept_as_text(self):
        """JSON nested too deeply to decode is kept as text instead of raising."""
        deep = "[" * 20000 + '{"type":"text","text":"x"}' + "]" * 20000
        result = index._extract_text_from_content_blocks("before " + deep + " after")
        self.assertTrue(result.startswith("before "), result[:40])
        self.assertTrue(result.endswith(" after"), result[-40:])
        chain = '[{"a":' * 20000 + "1"
        self.assertEqual(index._extract_text_from_content_blocks(chain), chain)

    def test_decode_attempts_are_capped(self):
        """Text with many '[{' candidates tries JSON decoding a bounded number of times."""
        calls = []
        real_decoder = index.json.JSONDecoder

        class CountingDecoder(real_decoder):
            def raw_decode(self, s, idx=0):
                calls.append(idx)
                return super().raw_decode(s, idx)

        original = index.json.JSONDecoder
        index.json.JSONDecoder = CountingDecoder
        try:
            text = '[{"type":0}' * 5000 + "]"
            self.assertEqual(index._extract_text_from_content_blocks(text), text)
        finally:
            index.json.JSONDecoder = original
        self.assertLessEqual(len(calls), index._MAX_DECODE_ATTEMPTS)

    def test_many_candidates_finish_quickly(self):
        """Large replies full of '[{' do not take time quadratic in their length."""
        import time
        for text in ("[{" * 250000, '[{"type":0}' * 45000 + "]",
                     '[{"type":"text","text":"\\q"}' * 16000 + "]"):
            start = time.monotonic()
            index._extract_text_from_content_blocks(text)
            self.assertLess(time.monotonic() - start, 15.0)

    def test_content_block_after_many_brackets_is_unwrapped(self):
        """'[{' that cannot start JSON does not use up the decode budget."""
        text = "[{" * 5000 + ' [{"type":"text","text":"kept"}]'
        result = index._extract_text_from_content_blocks(text)
        self.assertTrue(result.endswith(" kept"), result[-20:])


if __name__ == "__main__":
    unittest.main()
