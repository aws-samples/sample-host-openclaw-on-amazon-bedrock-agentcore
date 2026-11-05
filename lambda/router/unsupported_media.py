"""Messages the Router cannot read yet: which kinds, and what the agent is told.

A voice note, video, sticker, location or file that the Router cannot pass to
the agent gets a fixed notice (``index.unsupported_reply``) and runs no turn.
Media sent with text runs the text turn, and ``note()`` adds one line for the
agent saying what else was sent, so it can tell the user instead of guessing.
These helpers are pure (no AWS or network calls) so the rules can be tested on
their own. Logs name the kind only (``log_kind``), never file names, captions
or other content.
"""

import re

# Telegram message fields in the order they are checked; the first one present
# names the kind. A venue message also carries `location`, so it counts as one.
TELEGRAM_KINDS = ("voice", "audio", "video", "video_note", "sticker", "location", "venue", "contact", "poll", "dice")
# In a Telegram group these are reactions rather than requests: no notice.
TELEGRAM_GROUP_SILENT_KINDS = frozenset({"sticker", "dice"})
# Feishu message types that no person wrote to the bot: no notice.
FEISHU_SILENT_TYPES = frozenset({"system"})

_LABELS = {
    "voice": "a voice message",
    "audio": "an audio file",
    "video": "a video",
    "video_note": "a video message",
    "sticker": "a sticker",
    "location": "a location",
    "venue": "a location",
    "contact": "a contact",
    "contacts": "a contact",
    "poll": "a poll",
    "dice": "an animated emoji",
    "document": "a document",
    "file": "a file",
}

_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")
_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def label(kind):
    """How the note names one item of a kind, for example "a voice message"."""
    return _LABELS.get(kind, "a file")


def note(what, text_only=True):
    """The line appended to the agent's text, naming what else was sent.

    text_only=False when an image or a document was received as well as the text.
    """
    tail = " Only the text above was received." if text_only else ""
    return f"\n\n[The user also sent {what}, which cannot be read yet.{tail}]"


def skipped_note(what):
    """The line for files of a readable type that were not read: one file is read per message."""
    return (f"\n\n[The user also sent {what}, which was not read: only one file per message is read. "
            "The user can send the others in separate messages.]")


def telegram_kind(message):
    """The first of TELEGRAM_KINDS present in a Telegram message, or None."""
    return next((kind for kind in TELEGRAM_KINDS if message.get(kind)), None)


def mime_type(value):
    """A MIME type in lower case without parameters, or "" when the value is not one."""
    mime = str(value or "").split(";", 1)[0].strip().lower()
    return mime if _MIME_RE.fullmatch(mime) else ""


def log_kind(value):
    """A message type or MIME type that is safe to log, or "other"."""
    text = str(value or "").lower()
    return text if _KIND_RE.fullmatch(text) or _MIME_RE.fullmatch(text) else "other"


def files_label(mime_types):
    """Name files for the note: "a file (application/pdf)", "2 files (audio/webm, video/mp4)"."""
    types = list(dict.fromkeys(mime_type(mime) or "unknown type" for mime in mime_types))
    head = "a file" if len(mime_types) == 1 else f"{len(mime_types)} files"
    return f"{head} ({', '.join(types)})"


# Feishu rich text (post): elements kept as text, and elements left out with a note.
_FEISHU_TEXT_TAGS = frozenset({"text", "md", "code_block"})
_FEISHU_DROPPED_TAGS = {"img": ("an image", "images"), "media": ("a video", "videos")}
# In a received post, a mention is the placeholder also listed in message.mentions.
_FEISHU_MENTION_RE = re.compile(r"@_user_[0-9]{1,4}|@_all")
# A mention inside Markdown text names the user's ID: keep only the display name.
_FEISHU_MD_MENTION_RE = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)


def _feishu_element_text(element):
    tag = element.get("tag")
    if tag in _FEISHU_TEXT_TAGS:
        text = str(element.get("text") or "")
        return _FEISHU_MD_MENTION_RE.sub(r"@\1", text) if tag == "md" else text
    if tag == "a":
        text, href = str(element.get("text") or ""), str(element.get("href") or "")
        if not href.startswith(("https://", "http://")):
            return text
        return f"{text} ({href})" if text and text != href else href
    if tag == "at":
        user_id = str(element.get("user_id") or "")
        return user_id if _FEISHU_MENTION_RE.fullmatch(user_id) else ""
    return ""  # emotion, hr, img, media and unknown tags


def flatten_feishu_post(content):
    """A Feishu rich-text (post) message as plain text.

    Returns (text, left_out): left_out names the images and videos that were
    not passed on ("an image", "2 images and a video"), or is "". Text and
    Markdown elements and code blocks keep their text, a link becomes
    "text (href)" for an http(s) href, a mention becomes its placeholder
    (@_user_1) so the usual mention handling removes the bot's, and emoji are
    left out. A post wrapped in one language key ({"zh_cn": {...}}) is unwrapped.
    """
    if not isinstance(content, dict):
        return "", ""
    if "content" not in content and len(content) == 1:
        (inner,) = content.values()
        content = inner if isinstance(inner, dict) else {}
    title = content.get("title")
    lines = [title.strip()] if isinstance(title, str) and title.strip() else []
    left_out = dict.fromkeys(_FEISHU_DROPPED_TAGS, 0)
    paragraphs = content.get("content")
    for paragraph in paragraphs if isinstance(paragraphs, list) else []:
        parts = []
        for element in paragraph if isinstance(paragraph, list) else []:
            if not isinstance(element, dict):
                continue
            if element.get("tag") in left_out:
                left_out[element["tag"]] += 1
            parts.append(_feishu_element_text(element))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    names = [one if count == 1 else f"{count} {many}"
             for tag, (one, many) in _FEISHU_DROPPED_TAGS.items() if (count := left_out[tag])]
    return "\n".join(lines), " and ".join(names)
