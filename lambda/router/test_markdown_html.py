"""Tests for _markdown_to_telegram_html in the Router Lambda."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


class TestMarkdownToTelegramHtml(unittest.TestCase):
    """Tests for _markdown_to_telegram_html."""

    def test_plain_text_passthrough(self):
        self.assertEqual(index._markdown_to_telegram_html("Hello world"), "Hello world")

    def test_none_passthrough(self):
        self.assertIsNone(index._markdown_to_telegram_html(None))

    def test_empty_string_passthrough(self):
        self.assertEqual(index._markdown_to_telegram_html(""), "")

    # --- Bold ---

    def test_bold_double_asterisk(self):
        self.assertEqual(
            index._markdown_to_telegram_html("This is **bold** text"),
            "This is <b>bold</b> text",
        )

    def test_bold_double_underscore(self):
        self.assertEqual(
            index._markdown_to_telegram_html("This is __bold__ text"),
            "This is <b>bold</b> text",
        )

    # --- Italic ---

    def test_italic_single_asterisk(self):
        self.assertEqual(
            index._markdown_to_telegram_html("This is *italic* text"),
            "This is <i>italic</i> text",
        )

    # --- Strikethrough ---

    def test_strikethrough(self):
        self.assertEqual(
            index._markdown_to_telegram_html("This is ~~deleted~~ text"),
            "This is <s>deleted</s> text",
        )

    # --- Code ---

    def test_inline_code(self):
        self.assertEqual(
            index._markdown_to_telegram_html("Use `git status` command"),
            "Use <code>git status</code> command",
        )

    def test_code_block(self):
        result = index._markdown_to_telegram_html("```python\nprint('hello')\n```")
        self.assertIn("<pre>", result)
        self.assertIn("print('hello')", result)
        self.assertIn("</pre>", result)

    def test_code_block_html_escaped(self):
        result = index._markdown_to_telegram_html("```\n<div>test</div>\n```")
        self.assertIn("&lt;div&gt;", result)

    # --- Headers ---

    def test_h1(self):
        self.assertEqual(
            index._markdown_to_telegram_html("# Title"),
            "<b>Title</b>",
        )

    def test_h2(self):
        self.assertEqual(
            index._markdown_to_telegram_html("## Section"),
            "<b>Section</b>",
        )

    def test_h3_multiline(self):
        result = index._markdown_to_telegram_html("Text before\n### Header\nText after")
        self.assertEqual(result, "Text before\n<b>Header</b>\nText after")

    # --- Links ---

    def test_link(self):
        self.assertEqual(
            index._markdown_to_telegram_html("Visit [Google](https://google.com)"),
            'Visit <a href="https://google.com">Google</a>',
        )

    # --- Blockquotes ---

    def test_blockquote(self):
        result = index._markdown_to_telegram_html("> This is quoted")
        self.assertIn("<blockquote>", result)
        self.assertIn("This is quoted", result)

    # --- Horizontal rules ---

    def test_horizontal_rule(self):
        self.assertEqual(
            index._markdown_to_telegram_html("---"),
            "———",
        )

    # --- HTML escaping ---

    def test_html_entities_escaped(self):
        result = index._markdown_to_telegram_html("if a < b && c > d")
        self.assertIn("&lt;", result)
        self.assertIn("&gt;", result)
        self.assertIn("&amp;&amp;", result)

    def test_html_entities_not_double_escaped_in_code(self):
        result = index._markdown_to_telegram_html("`a < b`")
        # Should have exactly one level of escaping
        self.assertIn("&lt;", result)
        self.assertNotIn("&amp;lt;", result)

    # --- Combined / realistic ---

    def test_ai_response_formatting(self):
        text = (
            "# Quantum Computing Report\n\n"
            "**Key findings:**\n\n"
            "- Microsoft unveiled **Majorana 1** — a *topological* qubit processor\n"
            "- Use `quantum_sim` for simulation\n\n"
            "```python\nresult = simulate(qubits=8)\n```\n\n"
            "Visit [paper](https://arxiv.org) for details."
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("<b>Quantum Computing Report</b>", result)
        self.assertIn("<b>Key findings:</b>", result)
        self.assertIn("<b>Majorana 1</b>", result)
        self.assertIn("<i>topological</i>", result)
        self.assertIn("<code>quantum_sim</code>", result)
        self.assertIn("<pre>", result)
        self.assertIn('<a href="https://arxiv.org">paper</a>', result)

    def test_bullet_asterisk_not_italic(self):
        """Bullet points starting with * should not become italic."""
        text = "List:\n* item one\n* item two"
        result = index._markdown_to_telegram_html(text)
        # The * at start of line followed by space should NOT become <i>
        self.assertNotIn("<i>", result)

    # --- Tables (converted to bullet lists) ---

    def test_simple_table_to_bullets(self):
        """Markdown table is converted to bullet list."""
        text = (
            "| Name   | Age |\n"
            "|--------|-----|\n"
            "| Alice  | 30  |\n"
            "| Bob    | 25  |"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("•", result)
        self.assertIn("Alice", result)
        self.assertIn("Bob", result)
        # No table artifacts
        self.assertNotIn("|", result)
        self.assertNotIn("---", result)

    def test_table_separator_variations(self):
        """Various separator row styles are handled."""
        for sep in ["|---|---|", "|-----|-----|", "|:---|---:|", "|:---:|:---:|"]:
            text = f"| A | B |\n{sep}\n| x | y |"
            result = index._markdown_to_telegram_html(text)
            self.assertIn("•", result, f"Failed for separator: {sep}")
            self.assertNotIn("|---", result, f"Separator leaked for: {sep}")

    def test_table_header_only(self):
        """Table with header but no data rows shows header as bullets."""
        text = (
            "| Name | Value |\n"
            "|------|-------|\n"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("•", result)
        self.assertIn("Name", result)
        self.assertIn("Value", result)

    def test_single_col_table(self):
        """Single-column table data rows become bullets."""
        text = (
            "| Item |\n"
            "|------|\n"
            "| apple |\n"
            "| banana |"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("• apple", result)
        self.assertIn("• banana", result)

    def test_multi_col_table(self):
        """3+ column table joins cols with em dash."""
        text = (
            "| Name | Age | City |\n"
            "|------|-----|------|\n"
            "| Alice | 30 | NYC |\n"
            "| Bob | 25 | LA |"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("•", result)
        self.assertIn("Alice", result)
        self.assertIn("NYC", result)
        self.assertIn("—", result)

    def test_no_table_passthrough(self):
        """Text without tables passes through unchanged."""
        text = "No table here, just plain text."
        result = index._markdown_to_telegram_html(text)
        self.assertEqual(result, text)

    def test_table_with_bold(self):
        """Bold text in table cells is rendered as HTML bold."""
        text = (
            "| Language       | Speed |\n"
            "|----------------|-------|\n"
            "| **Python**     | Slow  |\n"
            "| **Rust**       | Fast  |"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("<b>Python</b>", result)
        self.assertIn("<b>Rust</b>", result)

    def test_table_with_surrounding_text(self):
        """Table embedded in text is converted; surrounding text is preserved."""
        text = (
            "Here is a comparison:\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n\n"
            "That's the table."
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("Here is a comparison:", result)
        self.assertIn("•", result)
        self.assertIn("That", result)

    def test_table_html_entities_escaped(self):
        """HTML special chars in bullet text are escaped."""
        text = (
            "| Expr   | Result |\n"
            "|--------|--------|\n"
            "| a < b  | true   |\n"
            "| c > d  | false  |"
        )
        result = index._markdown_to_telegram_html(text)
        self.assertIn("&lt;", result)
        self.assertIn("&gt;", result)


if __name__ == "__main__":
    unittest.main()
