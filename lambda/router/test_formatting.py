"""Tests for lambda/shared/python/formatting.py (Slack, WhatsApp, LINE and Feishu rendering)."""

import time
import unittest

import formatting

# (name, model text, Slack mrkdwn, WhatsApp, plain text for LINE and Feishu)
CASES = [
    ("heading and bold", "## Today\n**Rain** after 3 pm.",
     "*Today*\n*Rain* after 3 pm.", "*Today*\n*Rain* after 3 pm.", "Today\nRain after 3 pm."),
    ("heading markers", "# Title #\n### **Bold title**\n#hashtag",
     "*Title*\n*Bold title*\n#hashtag", "*Title*\n*Bold title*\n#hashtag", "Title\nBold title\n#hashtag"),
    ("link", "See [the forecast](https://example.com/f).",
     "See <https://example.com/f|the forecast>.", "See the forecast (https://example.com/f).",
     "See the forecast (https://example.com/f)."),
    ("link labelled with its URL", "[https://example.com](https://example.com)",
     "<https://example.com|https://example.com>", "https://example.com", "https://example.com"),
    ("link with parentheses", "[Foo](https://en.wikipedia.org/wiki/Foo_(bar))",
     "<https://en.wikipedia.org/wiki/Foo_(bar)|Foo>", "Foo (https://en.wikipedia.org/wiki/Foo_(bar))",
     "Foo (https://en.wikipedia.org/wiki/Foo_(bar))"),
    ("image", "![chart](https://example.com/c.png)",
     "<https://example.com/c.png|chart>", "chart (https://example.com/c.png)", "chart (https://example.com/c.png)"),
    ("bold label", "[**docs**](https://example.com)",
     "<https://example.com|docs>", "docs (https://example.com)", "docs (https://example.com)"),
    ("italic and strike", "*soft*, _also_ and ~~old~~ new",
     "_soft_, _also_ and ~old~ new", "_soft_, _also_ and ~old~ new", "soft, also and old new"),
    ("lists", "- one\n* two\n  + three\n1. four",
     "\u2022 one\n\u2022 two\n  \u2022 three\n1. four", "\u2022 one\n\u2022 two\n  \u2022 three\n1. four",
     "\u2022 one\n\u2022 two\n  \u2022 three\n1. four"),
    ("rule", "a\n---\nb\n* * *", "a\n\u2014\u2014\u2014\nb\n\u2014\u2014\u2014",
     "a\n\u2014\u2014\u2014\nb\n\u2014\u2014\u2014", "a\n\u2014\u2014\u2014\nb\n\u2014\u2014\u2014"),
    ("quote", "> quoted *it*", ">quoted _it_", "> quoted _it_", "> quoted it"),
    ("table", "| Name | Value |\n|---|---|\n| foo | 1 |\n| bar | 2 |",
     "\u2022 *foo* \u2014 1\n\u2022 *bar* \u2014 2", "\u2022 *foo* \u2014 1\n\u2022 *bar* \u2014 2",
     "\u2022 foo \u2014 1\n\u2022 bar \u2014 2"),
    ("inline code", "Run `rm -rf *_x_* <dir>` now",
     "Run `rm -rf *_x_* &lt;dir&gt;` now", "Run `rm -rf *_x_* <dir>` now", "Run rm -rf *_x_* <dir> now"),
    ("code block", "```python\nif a < b and c & d:\n    print('**x**')\n```\nafter",
     "```\nif a &lt; b and c &amp; d:\n    print('**x**')\n```\nafter",
     "```if a < b and c & d:\n    print('**x**')```\nafter",
     "if a < b and c & d:\n    print('**x**')\nafter"),
    ("unclosed fence", "```\nno end *here*", "```\nno end _here_", "```\nno end _here_", "```\nno end here"),
    ("words and arithmetic", "snake_case, file_name_v2 and 2 * 3 * 4",
     "snake_case, file_name_v2 and 2 * 3 * 4", "snake_case, file_name_v2 and 2 * 3 * 4",
     "snake_case, file_name_v2 and 2 * 3 * 4"),
    ("bare URLs", "Go to https://example.com/a_b_c?x=1&y=2, or **https://example.com/b**.",
     "Go to https://example.com/a_b_c?x=1&amp;y=2, or *https://example.com/b*.",
     "Go to https://example.com/a_b_c?x=1&y=2, or *https://example.com/b*.",
     "Go to https://example.com/a_b_c?x=1&y=2, or https://example.com/b."),
    ("special characters", "Q&A: 1 < 2 > 0", "Q&amp;A: 1 &lt; 2 &gt; 0", "Q&A: 1 < 2 > 0", "Q&A: 1 < 2 > 0"),
    ("plain", "Hello, world", "Hello, world", "Hello, world", "Hello, world"),
]


class TestRendering(unittest.TestCase):
    def test_each_channel(self):
        for name, text, slack, whatsapp, plain in CASES:
            with self.subTest(name):
                self.assertEqual(formatting.slack_mrkdwn(text), slack)
                self.assertEqual(formatting.whatsapp_text(text), whatsapp)
                self.assertEqual(formatting.plain_text(text), plain)
                self.assertEqual(formatting.feishu_text(text), plain)

    def test_empty_and_non_text_pass_through(self):
        for render in (formatting.slack_mrkdwn, formatting.whatsapp_text,
                       formatting.plain_text, formatting.feishu_text):
            with self.subTest(render.__name__):
                self.assertEqual(render(""), "")
                self.assertIsNone(render(None))

    def test_input_control_characters_cannot_reach_placeholders(self):
        text = "a\x000\x00b \x02bold\x02 \x05"
        self.assertEqual(formatting.slack_mrkdwn("[x](https://e.example) " + text),
                         "<https://e.example|x> a0b bold ")


class TestMentionAndLinkInjection(unittest.TestCase):
    def test_slack_mentions_from_model_text_are_escaped(self):
        for text in ("<!channel>", "<!here>", "<!everyone>", "<@U1>", "<@U1|someone>", "<#C1>",
                     "<!subteam^S1|team>", "<!date^1392734382^{date}|x>", "<https://evil.example|click me>",
                     "&lt;!channel&gt;"):
            with self.subTest(text):
                out = formatting.slack_mrkdwn(f"hi {text} **now**")
                self.assertNotIn("<", out)
                self.assertNotIn(">", out)
                self.assertTrue(out.startswith("hi &"), out)

    def test_mentions_in_a_link_label_are_escaped(self):
        self.assertEqual(formatting.slack_mrkdwn("[<!channel> <@U1>](https://example.com)"),
                         "<https://example.com|&lt;!channel&gt; &lt;@U1&gt;>")

    def test_only_plain_http_urls_become_links(self):
        for url in ("javascript:alert(1)", "JavaScript:alert(document.cookie)", "data:text/html,<b>x</b>",
                    "vbscript:msgbox", "file:///etc/passwd", "slack://open", "https:///no-host",
                    "https://e.example|<!channel>", "https://e.example/<!here>", "https://e.example/\"q\""):
            with self.subTest(url):
                text = f"[click]({url}) done"
                self.assertEqual(formatting.slack_mrkdwn(text), "click done")
                self.assertEqual(formatting.whatsapp_text(text), "click done")
                self.assertEqual(formatting.plain_text(text), "click done")

    def test_slack_link_parts_are_escaped(self):
        self.assertEqual(formatting.slack_mrkdwn("[R&D](https://e.example/?a=1&b=2)"),
                         "<https://e.example/?a=1&amp;b=2|R&amp;D>")

    def test_feishu_at_tags_are_removed(self):
        for text, expected in (('<at user_id="all">everyone</at> hi', "everyone hi"),
                               ('<AT user_id=all></AT>ping', "ping"),
                               ('`<at user_id="all"></at>` in code', " in code"),
                               ("<atom> and <at>x</at>", "<atom> and x")):
            with self.subTest(text):
                self.assertEqual(formatting.feishu_text(text), expected)
        self.assertIn("<at", formatting.plain_text('<at user_id="all"></at>'))


class TestLinearTime(unittest.TestCase):
    """Pathological 200 KB inputs render quickly: no pattern backtracks over the rest of the text."""

    N = 200_000
    LIMIT_SECONDS = 1.0

    def test_pathological_inputs_finish_quickly(self):
        n = self.N
        inputs = {
            "stars": "*" * n, "bold opens": "**a " * (n // 4), "underscores": "_a " * (n // 3),
            "double underscores": "__a " * (n // 4), "tildes": "~~a " * (n // 4), "brackets": "[" * n,
            "open label": "[" + "a" * n, "open URL": "[a](" + "b" * n, "parentheses": "[a](" + "(" * n,
            "open fence": "```" + "a" * n, "fences": "```a" * (n // 4), "backticks": "`a" * (n // 2),
            "open backtick": "`" + "a" * n, "URL closers": "http://e.example" + ")" * n,
            "URL dots": "http://e.example/" + "." * n, "at tags": "<at " * (n // 4), "open at tag": "<at " + "a" * n,
            "heading": "# " + "#" * n, "rule-like": "- " * (n // 2) + "x", "table": "| a " * (n // 4) + "|",
            "quotes": "> " * (n // 2), "spaced stars": "*" + " " * n + "*", "lines": "\n" * n,
            "mixed": "**_~~`[(<at |#>" * (n // 15),
        }
        for name, text in inputs.items():
            for render in (formatting.slack_mrkdwn, formatting.whatsapp_text, formatting.feishu_text):
                with self.subTest(name, render=render.__name__):
                    start = time.perf_counter()
                    render(text)
                    self.assertLess(time.perf_counter() - start, self.LIMIT_SECONDS)


class TestSendResponses(unittest.TestCase):
    def test_slack_error(self):
        for raw, expected in ((b'{"ok": true}', None),
                              (b'{"ok": true, "warning": "superfluous_charset"}', None),
                              (b'{"ok": false, "error": "not_in_channel"}', "not_in_channel"),
                              (b'{"ok": false, "error": "channel_not_found", "response_metadata": {}}',
                               "channel_not_found"),
                              (b'{"ok": false, "error": "Text with\\nnewline and spaces"}', "unknown_error"),
                              (b'{"ok": false, "error": 5}', "unknown_error"),
                              (b'{"ok": false}', "unknown_error"),
                              (b'{"ok": "true"}', "unknown_error"),
                              (b"<html>502</html>", "unreadable_response"),
                              (b"[]", "unreadable_response"),
                              (b"", "unreadable_response"),
                              (None, "unreadable_response")):
            with self.subTest(raw=raw):
                self.assertEqual(formatting.slack_error(raw), expected)

    def test_feishu_error(self):
        for raw, expected in ((b'{"code": 0, "msg": "success", "data": {}}', None),
                              (b'{"code": 230002, "msg": "Bot is not in the chat"}', 230002),
                              (b'{"code": 99991663, "msg": "invalid token"}', 99991663),
                              (b'{"code": true}', "unknown_error"),
                              (b'{"code": "0"}', "unknown_error"),
                              (b'{"msg": "no code"}', "unknown_error"),
                              (b"<html>502</html>", "unreadable_response"),
                              (None, "unreadable_response")):
            with self.subTest(raw=raw):
                self.assertEqual(formatting.feishu_error(raw), expected)


if __name__ == "__main__":
    unittest.main()
