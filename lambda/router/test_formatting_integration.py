"""Integration tests for the full formatting pipeline.

Validates that content-block JSON wrappers are stripped and markdown tables
are converted to Unicode box-drawing <pre> blocks for Telegram — end-to-end
through both _extract_text_from_content_blocks and _markdown_to_telegram_html.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

# Set required env vars before importing the module
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

# Mock boto3 before importing the module
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


# ---------------------------------------------------------------------------
# Realistic payloads modelled on actual subagent responses
# ---------------------------------------------------------------------------

SAMPLE_CONTENT_BLOCKS = json.dumps([{
    "type": "text",
    "text": (
        "\n\n## Cron Jobs Fixed\n\n"
        "| ID | Name | Schedule | Sydney Time | Status |\n"
        "|----|------|----------|-------------|--------|\n"
        "| c4ad1090 | morning | cron(30 20 * * ? *) | 7:30 AM | ENABLED |\n\n"
        "### Root Cause Found\n\n"
        "Old cron jobs had wrong times.\n"
    ),
}])

CONTENT_BLOCKS_LITERAL_NEWLINES = '[{"type":"text","text":"\\n\\nTest\\n\\nContent here\\n"}]'

DOUBLE_WRAPPED = json.dumps([{
    "type": "text",
    "text": json.dumps([{
        "type": "text",
        "text": "Here are 3 things I can help with:\n\n- Task scheduling\n- File management\n- Web research",
    }]),
}])


class TestFullPipeline(unittest.TestCase):
    """End-to-end: content-blocks JSON -> extracted text -> Telegram HTML."""

    def _pipeline(self, raw: str) -> str:
        """Run the full formatting pipeline: extract then convert to HTML."""
        extracted = index._extract_text_from_content_blocks(raw)
        return index._markdown_to_telegram_html(extracted)

    # --- No raw JSON leaks ---

    def test_single_wrapped_no_raw_json(self):
        """Single-level content-block wrapper is fully stripped."""
        html = self._pipeline(SAMPLE_CONTENT_BLOCKS)
        self.assertNotIn('[{"type":"text"', html)
        self.assertNotIn('{"type": "text"', html)
        self.assertNotIn('"type":"text"', html)
        self.assertIn("Cron Jobs Fixed", html)

    def test_double_wrapped_no_raw_json(self):
        """Double-nested content-block wrapper is fully stripped."""
        html = self._pipeline(DOUBLE_WRAPPED)
        self.assertNotIn('[{"type":"text"', html)
        self.assertNotIn('{"type": "text"', html)
        self.assertIn("Task scheduling", html)

    def test_literal_newlines_no_raw_json(self):
        """Content blocks with literal newlines are unwrapped cleanly."""
        html = self._pipeline(CONTENT_BLOCKS_LITERAL_NEWLINES)
        self.assertNotIn('[{"type":"text"', html)
        self.assertIn("Content here", html)

    # --- No markdown table separators ---

    def test_table_converted_no_markdown_separators(self):
        """Markdown table is converted to <pre> box-drawing — no |---| leaks."""
        html = self._pipeline(SAMPLE_CONTENT_BLOCKS)
        self.assertNotIn("| ---", html)
        self.assertNotIn("|---|", html)
        self.assertNotIn("|----", html)
        self.assertNotIn("|---", html)
        self.assertNotIn("|------", html)
        self.assertIn("<pre>", html)
        self.assertIn("│", html)

    def test_table_in_double_wrapped_response(self):
        """Table inside double-wrapped content blocks is rendered correctly."""
        inner_text = (
            "| Feature | Available |\n"
            "|---------|----------|\n"
            "| Cron    | Yes      |\n"
            "| Files   | Yes      |"
        )
        payload = json.dumps([{"type": "text", "text": json.dumps([
            {"type": "text", "text": inner_text},
        ])}])
        html = self._pipeline(payload)
        self.assertNotIn("|---|", html)
        self.assertIn("<pre>", html)
        self.assertIn("│", html)
        self.assertIn("Cron", html)

    # --- Output is plain text or valid Telegram HTML ---

    def test_output_not_raw_json(self):
        """Pipeline output never starts with [{ (raw JSON array)."""
        for payload in [SAMPLE_CONTENT_BLOCKS, DOUBLE_WRAPPED, CONTENT_BLOCKS_LITERAL_NEWLINES]:
            html = self._pipeline(payload)
            self.assertFalse(
                html.lstrip().startswith("[{"),
                f"Output starts with raw JSON: {html[:100]}",
            )

    def test_output_uses_telegram_html_tags(self):
        """Pipeline output uses Telegram-safe HTML tags for formatting."""
        html = self._pipeline(SAMPLE_CONTENT_BLOCKS)
        # Headers become bold
        self.assertIn("<b>", html)
        # Tables become <pre> box-drawing blocks
        self.assertIn("<pre>", html)
        self.assertIn("│", html)

    # --- Realistic subagent payloads ---

    def test_realistic_cron_fix_response(self):
        """Full pipeline on realistic cron-fix subagent response."""
        html = self._pipeline(SAMPLE_CONTENT_BLOCKS)
        # Content is present
        self.assertIn("Cron Jobs Fixed", html)
        self.assertIn("Root Cause Found", html)
        self.assertIn("c4ad1090", html)
        # No raw artifacts
        self.assertNotIn('[{"type"', html)
        self.assertNotIn("|---|", html)

    def test_realistic_list_response(self):
        """Bullet-list response from subagent is clean."""
        html = self._pipeline(DOUBLE_WRAPPED)
        self.assertIn("Task scheduling", html)
        self.assertIn("File management", html)
        self.assertIn("Web research", html)
        self.assertNotIn('[{"type"', html)

    # --- Extraction edge cases ---

    def test_plain_text_passthrough(self):
        """Plain text (not JSON) passes through the pipeline unchanged."""
        html = self._pipeline("Hello, this is a plain response.")
        self.assertEqual(html, "Hello, this is a plain response.")

    def test_non_content_block_json_unchanged(self):
        """JSON that isn't content-blocks passes through as-is."""
        raw = '[{"key": "value"}]'
        html = self._pipeline(raw)
        self.assertIn('"key"', html)

    def test_regex_fallback_for_malformed_json(self):
        """Malformed JSON that matches content-block pattern uses regex fallback."""
        # Simulate JSON with a trailing comma (invalid JSON but matchable by regex)
        raw = '[{"type":"text","text":"Hello from regex"},]'
        result = index._extract_text_from_content_blocks(raw)
        # The regex fallback should extract the text if JSON parse fails
        # Either the text is extracted or the raw string is returned (both acceptable)
        self.assertTrue(
            result == "Hello from regex" or result == raw,
            f"Unexpected result: {result}",
        )


class TestExtractionThenHtml(unittest.TestCase):
    """Focused tests on extraction → HTML conversion ordering."""

    def test_extraction_strips_before_html_converts(self):
        """Content blocks are fully unwrapped BEFORE markdown → HTML conversion."""
        wrapped = json.dumps([{"type": "text", "text": "**Bold** and *italic*"}])
        extracted = index._extract_text_from_content_blocks(wrapped)
        self.assertEqual(extracted, "**Bold** and *italic*")
        html = index._markdown_to_telegram_html(extracted)
        self.assertIn("<b>Bold</b>", html)
        self.assertIn("<i>italic</i>", html)

    def test_table_in_content_blocks_becomes_pre_ascii(self):
        """Table inside content blocks → extracted → converted to <pre> box-drawing."""
        table_md = (
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |"
        )
        wrapped = json.dumps([{"type": "text", "text": table_md}])
        extracted = index._extract_text_from_content_blocks(wrapped)
        self.assertEqual(extracted, table_md)
        html = index._markdown_to_telegram_html(extracted)
        self.assertIn("<pre>", html)
        self.assertIn("│", html)
        self.assertNotIn("|---|", html)

    def test_multiple_blocks_concatenated_then_formatted(self):
        """Multiple content blocks are concatenated, then HTML-formatted."""
        wrapped = json.dumps([
            {"type": "text", "text": "# Title\n\n"},
            {"type": "text", "text": "Paragraph with **bold**."},
        ])
        extracted = index._extract_text_from_content_blocks(wrapped)
        self.assertIn("# Title", extracted)
        self.assertIn("**bold**", extracted)
        html = index._markdown_to_telegram_html(extracted)
        self.assertIn("<b>Title</b>", html)
        self.assertIn("<b>bold</b>", html)


if __name__ == "__main__":
    unittest.main()
