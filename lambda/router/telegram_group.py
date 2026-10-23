"""Telegram group chats: which messages the bot answers, and as whom.

A group (or supergroup) is served as its own identity, never as the member who
sent the message: actor ``telegram_group:<chat_id>``, with its own namespace,
session, memory and files. The sender's name and ID reach the agent only as a
line of context in front of the message.

The router answers a group message only when all of these hold:
- the ``telegram_groups_enabled`` setting is on;
- the group is allowlisted (``ALLOW#telegram_group:<chat_id>``);
- the message is addressed to the bot: it @mentions the bot, replies to one of
  the bot's own messages, or is a ``/command@BotName``.

Everything else in a group is ignored without a reply. These helpers are pure
(no AWS or network calls) so the rules can be tested on their own.
"""

import re

CHANNEL = "telegram_group"
GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
# getChatMember statuses that may allowlist a group.
ADMIN_STATUSES = frozenset({"creator", "administrator"})
# The admin command, sent in the group as /allowgroup@BotName.
ALLOW_COMMAND = "allowgroup"

# Group and supergroup chat IDs are negative integers.
GROUP_ID_RE = re.compile(r"-[0-9]{1,19}")
USER_ID_RE = re.compile(r"[0-9]{1,19}")
BOT_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
_ADDRESSED_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})@([A-Za-z0-9_]{1,64})(?=\s|$)(.*)$", re.DOTALL)
_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:\s+(.*))?$", re.DOTALL)
# Characters kept from a sender's display name: letters and digits in any
# script, space and . ' -. Brackets, colons, @ and line breaks are dropped so a
# name cannot pose as a header or another identity.
_NAME_DROP_RE = re.compile(r"[^\w .'-]|_")
MAX_SENDER_NAME = 48


def chat_kind(message):
    """Return "private", "group" or "other" for a Telegram message.

    Telegram always sends chat.type. A chat without a type keeps the old
    handling (private) unless its ID is negative, which only group and
    channel chats have; those are handled as a group.
    """
    chat = message.get("chat") or {}
    chat_type = chat.get("type")
    if chat_type in GROUP_CHAT_TYPES:
        return "group"
    if chat_type == "private":
        return "private"
    if chat_type is None:
        return "group" if str(chat.get("id", "")).startswith("-") else "private"
    return "other"


def bot_id_from_token(token):
    """The bot's numeric user ID: the part of the bot token before the colon."""
    if not isinstance(token, str):
        return None
    prefix = token.split(":", 1)[0]
    return int(prefix) if USER_ID_RE.fullmatch(prefix) else None


def is_reply_to_bot(message, bot_id):
    """True when the message replies to one of this bot's own messages."""
    reply = message.get("reply_to_message")
    if bot_id is None or not isinstance(reply, dict):
        return False
    # In a forum topic every message points at the topic's first message;
    # that is not a reply to the bot.
    if message.get("is_topic_message") and reply.get("message_id") == message.get("message_thread_id"):
        return False
    sender = reply.get("from") or {}
    return sender.get("is_bot") is True and sender.get("id") == bot_id


def addressed_text(message, text, bot_id, bot_username):
    """Return the message text addressed to this bot, or None if it is not.

    - "/cmd@BotName args" becomes "/cmd args"; a command for another bot is None.
    - "@BotName ..." has every mention of the bot removed.
    - A reply to one of the bot's messages is returned unchanged.
    A bare "/cmd" without the bot's name is not addressed to the bot.
    Without the bot's username only replies can be matched.
    """
    text = text if isinstance(text, str) else ""
    command = _ADDRESSED_COMMAND_RE.match(text.lstrip())
    if command:
        if bot_username and command.group(2).lower() == bot_username.lower():
            return ("/" + command.group(1) + command.group(3)).strip()
        return None
    if bot_username:
        mention = re.compile(
            r"(?<![A-Za-z0-9_@])@" + re.escape(bot_username) + r"(?![A-Za-z0-9_])", re.IGNORECASE,
        )
        if mention.search(text):
            stripped = mention.sub("", text)
            return "\n".join(" ".join(line.split()) for line in stripped.split("\n")).strip()
    if is_reply_to_bot(message, bot_id):
        return text.strip()
    return None


def parse_command(text):
    """Return (command, arguments) for "/command args", else (None, "")."""
    match = _COMMAND_RE.match(text.strip()) if isinstance(text, str) else None
    if not match:
        return None, ""
    return match.group(1).lower(), (match.group(2) or "").strip()


def sender_label(user):
    """'First Last (telegram:<id>)' with the name reduced to safe characters."""
    parts = [user.get(key) for key in ("first_name", "last_name")]
    name = " ".join(part for part in parts if isinstance(part, str) and part)
    name = " ".join(_NAME_DROP_RE.sub(" ", name).split())[:MAX_SENDER_NAME].strip() or "unknown"
    user_id = str(user.get("id", ""))
    return f"{name} (telegram:{user_id})" if USER_ID_RE.fullmatch(user_id) else name


def agent_text(user, text):
    """The text sent to the agent: one context line naming the sender, then the message."""
    return f"Telegram group message from {sender_label(user)}:\n{text}"
