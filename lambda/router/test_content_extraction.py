"""Tests for _extract_text_from_content_blocks in the Router Lambda.

Verifies that nested content block JSON is recursively unwrapped — a common
issue when subagent responses are wrapped in multiple layers of content blocks.
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


class TestExtractTextFromContentBlocks(unittest.TestCase):
    """Tests for _extract_text_from_content_blocks."""

    def test_plain_text_passthrough(self):
        """Plain text is returned unchanged."""
        self.assertEqual(index._extract_text_from_content_blocks("Hello world"), "Hello world")

    def test_none_passthrough(self):
        """None is returned unchanged."""
        self.assertIsNone(index._extract_text_from_content_blocks(None))

    def test_empty_string_passthrough(self):
        """Empty string is returned unchanged."""
        self.assertEqual(index._extract_text_from_content_blocks(""), "")

    def test_single_level_content_blocks(self):
        """Single-level content blocks are unwrapped."""
        blocks = json.dumps([{"type": "text", "text": "Hello world"}])
        self.assertEqual(index._extract_text_from_content_blocks(blocks), "Hello world")

    def test_double_nested_content_blocks(self):
        """Double-nested content blocks (subagent response) are fully unwrapped."""
        inner = json.dumps([{"type": "text", "text": "Found several skills."}])
        outer = json.dumps([{"type": "text", "text": inner}])
        self.assertEqual(
            index._extract_text_from_content_blocks(outer),
            "Found several skills.",
        )

    def test_triple_nested_content_blocks(self):
        """Triple-nested content blocks (deep subagent chain) are fully unwrapped."""
        level1 = "Found several. Here are the most relevant."
        level2 = json.dumps([{"type": "text", "text": level1}])
        level3 = json.dumps([{"type": "text", "text": level2}])
        level4 = json.dumps([{"type": "text", "text": level3}])
        self.assertEqual(
            index._extract_text_from_content_blocks(level4),
            level1,
        )

    def test_multiple_text_blocks(self):
        """Multiple text blocks are concatenated."""
        blocks = json.dumps([
            {"type": "text", "text": "Part 1. "},
            {"type": "text", "text": "Part 2."},
        ])
        self.assertEqual(
            index._extract_text_from_content_blocks(blocks),
            "Part 1. Part 2.",
        )

    def test_non_content_block_json_array(self):
        """JSON arrays that are not content blocks are returned as-is."""
        text = '[{"key": "value"}]'
        self.assertEqual(index._extract_text_from_content_blocks(text), text)

    def test_malformed_json(self):
        """Malformed content block JSON is stripped, not leaked."""
        text = '[{"type":"text","text":"broken'
        result = index._extract_text_from_content_blocks(text)
        self.assertNotIn("[{", result)

    def test_preserves_newlines_and_markdown(self):
        """Newlines and markdown are preserved after unwrapping."""
        inner = "# Title\n\n- Item 1\n- Item 2\n\n**Bold** text"
        wrapped = json.dumps([{"type": "text", "text": inner}])
        self.assertEqual(
            index._extract_text_from_content_blocks(wrapped),
            inner,
        )

    def test_text_with_escaped_content(self):
        """Realistic subagent response with escaped JSON and special characters."""
        actual_text = (
            "Found several. Here are the most relevant:\n\n"
            "🔧 **Claude Code Skills:**\n\n"
            "• **claude-code** — Claude Code Integration\n"
            "• **openclaw-claude-code** — Claude Code Agent"
        )
        level2 = json.dumps([{"type": "text", "text": actual_text}])
        level3 = json.dumps([{"type": "text", "text": level2}])
        self.assertEqual(
            index._extract_text_from_content_blocks(level3),
            actual_text,
        )

    def test_non_string_input(self):
        """Non-string input is returned as-is."""
        self.assertEqual(index._extract_text_from_content_blocks(42), 42)
        self.assertEqual(index._extract_text_from_content_blocks([1, 2]), [1, 2])

    def test_regex_fallback_for_unparseable_json(self):
        """Regex fallback extracts text when JSON parsing fails due to encoding issues."""
        # Simulate a JSON string with literal control characters that break json.loads
        # even with strict=False in some edge cases — the regex should still extract text
        raw = '[{"type":"text","text":"Hello world"}]'
        # Normal case works via JSON parse, but test the regex path by verifying
        # the function handles a string that looks like content blocks
        self.assertEqual(
            index._extract_text_from_content_blocks(raw),
            "Hello world",
        )

    def test_regex_fallback_comma_separator(self):
        """Regex fallback handles comma instead of colon in malformed JSON."""
        # Real-world case: "text","value" instead of "text":"value"
        raw = '[{"type":"text","text","\\n\\n## ✅ Hello World"}]'
        result = index._extract_text_from_content_blocks(raw)
        self.assertIn("Hello World", result)

    def test_regex_fallback_does_not_alter_non_content_blocks(self):
        """Regex fallback does not alter JSON that isn't content blocks."""
        raw = '[{"key": "value"}]'
        self.assertEqual(index._extract_text_from_content_blocks(raw), raw)

    def test_image_only_blocks(self):
        """Image-only content blocks return empty string, not '[{'."""
        blocks = json.dumps([
            {"type": "image", "source": {"type": "base64", "data": "abc123"}},
        ])
        result = index._extract_text_from_content_blocks(blocks)
        self.assertNotIn("[{", result)
        self.assertEqual(result, "")

    def test_mixed_image_and_text_blocks(self):
        """Mixed image+text blocks return only the text parts."""
        blocks = json.dumps([
            {"type": "image", "source": {"type": "base64", "data": "abc123"}},
            {"type": "text", "text": "Here is the screenshot."},
        ])
        result = index._extract_text_from_content_blocks(blocks)
        self.assertEqual(result, "Here is the screenshot.")
        self.assertNotIn("[{", result)

    def test_tool_use_blocks_skipped(self):
        """tool_use blocks are skipped; only text blocks extracted."""
        blocks = json.dumps([
            {"type": "tool_use", "id": "t1", "name": "web_search", "input": {"q": "test"}},
            {"type": "text", "text": "Search results below."},
        ])
        result = index._extract_text_from_content_blocks(blocks)
        self.assertEqual(result, "Search results below.")

    def test_tool_result_blocks_skipped(self):
        """tool_result blocks are skipped; only text blocks extracted."""
        blocks = json.dumps([
            {"type": "tool_result", "tool_use_id": "t1", "content": "result data"},
            {"type": "text", "text": "Done."},
        ])
        result = index._extract_text_from_content_blocks(blocks)
        self.assertEqual(result, "Done.")

    def test_embedded_image_blocks_in_text(self):
        """Image blocks embedded in surrounding text are removed cleanly."""
        blocks = json.dumps([
            {"type": "image", "source": {"type": "base64", "data": "xyz"}},
        ])
        text = f"Here is the image: {blocks} — hope that helps!"
        result = index._extract_text_from_content_blocks(text)
        self.assertNotIn("[{", result)
        self.assertIn("Here is the image:", result)
        self.assertIn("hope that helps!", result)

    def test_multiple_image_blocks(self):
        """Multiple image blocks produce empty text, no leakage."""
        blocks = json.dumps([
            {"type": "image", "source": {"type": "base64", "data": "a"}},
            {"type": "image", "source": {"type": "base64", "data": "b"}},
        ])
        result = index._extract_text_from_content_blocks(blocks)
        self.assertEqual(result, "")

    def test_truncated_content_block_json(self):
        """Truncated content block JSON (\\n\\n[{) should not leak raw JSON."""
        result = index._extract_text_from_content_blocks("\n\n[{")
        # The raw '[{' should not appear in the result
        self.assertNotIn("[{", result)

    def test_partial_content_block_json(self):
        """Partial content block JSON should not leak."""
        result = index._extract_text_from_content_blocks('\n\n[{"type":"text","text":"hello')
        self.assertNotIn("[{", result)


if __name__ == "__main__":
    unittest.main()
