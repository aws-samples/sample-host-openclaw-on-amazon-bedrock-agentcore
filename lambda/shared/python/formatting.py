"""Render model replies (Markdown) for Slack, WhatsApp, LINE and Feishu.

Shared by the Router and Cron Lambdas. Telegram gets HTML from each Lambda's
own _markdown_to_telegram_html, and Discord renders Markdown itself.

- slack_mrkdwn: Slack mrkdwn. Every &, < and > in the model's text is
  escaped, so the text cannot form a Slack control sequence: no <!channel>,
  <!here>, <@U...>, <#C...> or <!subteam^...> mention, and no <url|label>
  link. The only links in the output are built here, from Markdown links
  whose URL is a plain http(s) URL.
- whatsapp_text: WhatsApp formatting (*bold*, _italic_, ~strike~, code);
  links become "label (url)".
- plain_text (LINE) and feishu_text: Markdown markers removed, links become
  "label (url)". feishu_text also removes <at> tags, which Feishu reads as
  @mentions in text messages.

In all four, headings become bold lines (plain lines in plain text), tables
become bullet lists and list markers become "•". Code is kept as it is. A
link whose URL is not http(s) (javascript:, data:, ...) keeps only its label.

slack_error and feishu_error read a send response and return only its error
code, so callers can log it without logging the body.

Every pattern scans in time linear in the text: emphasis, labels and tags
stop at the next delimiter instead of backtracking over the rest of a line.
Stdlib only; nothing here sends anything.
"""

import json
import re
from urllib.parse import urlparse

# Placeholders for pieces that later steps must not rewrite (code, links,
# URLs), and markers for formatting each channel spells differently. The
# input's own characters in this range are removed first.
_KEEP = "\x00"
_BOLD, _ITALIC, _STRIKE, _QUOTE = "\x02", "\x03", "\x04", "\x05"
_CONTROL_RE = re.compile(r"[\x00-\x05]")
_KEPT_RE = re.compile(r"\x00(\d+)\x00")

_FENCE = "```"
_FENCE_INFO_RE = re.compile(r"[\w+.#-]*")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# [label](url) or ![alt](url); the URL may hold one level of parentheses.
_LINK_RE = re.compile(r"!?\[([^\[\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"'`|\\]+")
_URL_RE = re.compile(r"[^\x00-\x20\x7f<>\"'`|\\]+")
_RULE_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(\S.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(?=\S)((?:[^*\n]|\*(?!\*))+?)(?<=\S)\*\*"
                      r"|__(?=\S)((?:[^_\n]|_(?!_))+?)(?<=\S)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"
                        r"|(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)((?:[^~\n]|~(?!~))+?)(?<=\S)~~")
_EMPHASIS_RE = re.compile(r"\*\*|__|~~")
_TABLE_DIVIDER_RE = re.compile(r"^\|[\s\-:|]+\|$")
_FEISHU_AT_RE = re.compile(r"</?\s*at(?:\s[^<>]*)?>", re.IGNORECASE)
_TRAILING_URL_CHARS = ".,;:!?*_~)"
_RULE = "\u2014\u2014\u2014"
_BULLET = "\u2022"
_DASH = " \u2014 "


def _slack_escape(text):
    """Slack's escaping: https://api.slack.com/reference/surfaces/formatting#escaping."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _http_url(url):
    """The URL if it is a plain http(s) URL that is safe inside <url|label>, else None."""
    if not _URL_RE.fullmatch(url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _heading_text(title):
    """A heading's text without trailing whitespace, closing #s or emphasis markers."""
    title = title.rstrip()
    stripped = title.rstrip("#")
    if stripped != title and (not stripped or stripped[-1] in " \t"):
        title = stripped.rstrip()
    return _EMPHASIS_RE.sub("", title)


def _replace_fences(text, replace):
    """Replace each ```...``` block with replace(body), dropping a language line.

    Fences are paired by scanning, not by a regex, so an unclosed fence costs
    one pass over the text.
    """
    out = []
    pos = 0
    while True:
        start = text.find(_FENCE, pos)
        end = text.find(_FENCE, start + 3) if start >= 0 else -1
        if end < 0:
            break
        body = text[start + 3:end]
        newline = body.find("\n")
        if newline >= 0 and _FENCE_INFO_RE.fullmatch(body[:newline]):
            body = body[newline + 1:]
        out.append(text[pos:start])
        out.append(replace(body))
        pos = end + 3
    out.append(text[pos:])
    return "".join(out)


def _split_trailing(url):
    """(url, trailing punctuation that belongs to the sentence, not the URL)."""
    opens, closes = url.count("("), url.count(")")
    end = len(url)
    while end and url[end - 1] in _TRAILING_URL_CHARS:
        if url[end - 1] == ")":
            if opens >= closes:
                break
            closes -= 1
        end -= 1
    return url[:end], url[end:]


def _tables_to_bullets(text):
    """Markdown tables as bullets, as the Telegram conversion does.

    | Name | Description |   ->   (header row dropped)
    | foo  | bar         |   ->   • **foo** — bar
    """
    if "|" not in text:
        return text
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not (stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2):
            out.append(lines[i])
            i += 1
            continue
        header_seen = False
        while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
            row = lines[i].strip()
            i += 1
            if _TABLE_DIVIDER_RE.match(row):
                continue
            cols = [c.strip() for c in row.strip("|").split("|")]
            cols = [c for c in cols if c]
            if not cols:
                continue
            if not header_seen:
                header_seen = True
            elif len(cols) == 1:
                out.append(f"{_BULLET} {cols[0]}")
            else:
                out.append(f"{_BULLET} **{cols[0]}**{_DASH}{_DASH.join(cols[1:])}")
    return "\n".join(out)


class _Plain:
    """Plain text (LINE, Feishu). The other channels override how pieces are spelled."""

    bold = italic = strike = ""
    quote = "> "

    def escape(self, text):
        return text

    def code_block(self, code):
        return code.rstrip("\n")

    def code(self, code):
        return code

    def link(self, label, url):
        if url is None:
            return label
        return url if label.strip() == url else f"{label} ({url})"

    def bare_url(self, url):
        return url


class _Slack(_Plain):
    bold, italic, strike, quote = "*", "_", "~", ">"

    def escape(self, text):
        return _slack_escape(text)

    def code_block(self, code):
        code = _slack_escape(code.rstrip("\n"))
        return f"```\n{code}\n```"

    def code(self, code):
        return f"`{_slack_escape(code)}`"

    def link(self, label, url):
        label = _slack_escape(label)
        return f"<{_slack_escape(url)}|{label}>" if url else label

    def bare_url(self, url):
        return _slack_escape(url)


class _WhatsApp(_Plain):
    bold, italic, strike = "*", "_", "~"

    def code_block(self, code):
        code = code.rstrip("\n")
        return f"```{code}```"

    def code(self, code):
        return f"`{code}`"


def _render(text, channel):
    if not isinstance(text, str) or not text:
        return text
    text = _CONTROL_RE.sub("", text)
    kept = []

    def keep(rendered):
        kept.append(rendered)
        return f"{_KEEP}{len(kept) - 1}{_KEEP}"

    def bare_url(match):
        url, trail = _split_trailing(match.group(0))
        valid = _http_url(url)
        # An invalid one stays text and is escaped with the rest below.
        return (keep(channel.bare_url(valid)) if valid else url) + trail

    def link(match):
        # The label may hold kept inline code; it is restored at the end.
        label = _EMPHASIS_RE.sub("", match.group(1))
        return keep(channel.link(label, _http_url(match.group(2))))

    def heading(match):
        title = _heading_text(match.group(1))
        return f"{_BOLD}{title}{_BOLD}" if title else ""

    # Code first, so nothing below rewrites it.
    text = _replace_fences(text, lambda body: keep(channel.code_block(body)))
    text = _tables_to_bullets(text)
    text = _INLINE_CODE_RE.sub(lambda m: keep(channel.code(m.group(1))), text)
    text = _LINK_RE.sub(link, text)
    text = _BARE_URL_RE.sub(bare_url, text)
    text = _QUOTE_RE.sub(_QUOTE, text)
    text = channel.escape(text)
    text = _RULE_RE.sub(_RULE, text)
    text = _HEADING_RE.sub(heading, text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}{_BULLET} ", text)
    text = _BOLD_RE.sub(lambda m: f"{_BOLD}{m.group(1) or m.group(2)}{_BOLD}", text)
    text = _ITALIC_RE.sub(lambda m: f"{_ITALIC}{m.group(1) or m.group(2)}{_ITALIC}", text)
    text = _STRIKE_RE.sub(lambda m: f"{_STRIKE}{m.group(1)}{_STRIKE}", text)
    for marker, spelling in ((_BOLD, channel.bold), (_ITALIC, channel.italic),
                             (_STRIKE, channel.strike), (_QUOTE, channel.quote)):
        text = text.replace(marker, spelling)
    # A kept link can hold kept inline code, so restore until none is left.
    for _ in range(3):
        text, count = _KEPT_RE.subn(lambda m: kept[int(m.group(1))], text)
        if not count:
            break
    return text


_SLACK, _WHATSAPP, _PLAIN = _Slack(), _WhatsApp(), _Plain()


def slack_mrkdwn(text):
    """Model Markdown as Slack mrkdwn, escaped so it cannot mention anyone or inject links."""
    return _render(text, _SLACK)


def whatsapp_text(text):
    """Model Markdown in WhatsApp's formatting; links as "label (url)"."""
    return _render(text, _WHATSAPP)


def plain_text(text):
    """Model Markdown as plain text (LINE); links as "label (url)"."""
    return _render(text, _PLAIN)


def feishu_text(text):
    """plain_text without <at> tags, so model text cannot @mention anyone in a Feishu chat."""
    text = plain_text(text)
    return _FEISHU_AT_RE.sub("", text) if isinstance(text, str) else text


# ---------------------------------------------------------------------------
# Send responses: the error code only, never the body
# ---------------------------------------------------------------------------

_SLACK_ERROR_RE = re.compile(r"[a-z0-9_]{1,64}")


def _json_object(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def slack_error(raw):
    """None when a Slack Web API response says ok, else Slack's error code.

    Slack answers HTTP 200 with {"ok": false, "error": "not_in_channel"} when
    it refuses a message. Only a short error code is ever returned.
    """
    data = _json_object(raw)
    if data is None:
        return "unreadable_response"
    if data.get("ok") is True:
        return None
    error = data.get("error")
    return error if isinstance(error, str) and _SLACK_ERROR_RE.fullmatch(error) else "unknown_error"


def feishu_error(raw):
    """None when a Feishu API response has code 0, else its numeric code.

    Feishu answers HTTP 200 with a non-zero "code" (and a "msg" that is never
    returned) when it refuses a message.
    """
    data = _json_object(raw)
    if data is None:
        return "unreadable_response"
    code = data.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return "unknown_error"
    return None if code == 0 else code
