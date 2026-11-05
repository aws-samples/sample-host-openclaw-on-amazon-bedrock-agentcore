"""Slack channels and Feishu group chats served as their own identity.

The ``channel_identity_mode`` setting (cdk.json) chooses who answers in a Slack
channel or a Feishu group chat:

- ``personal`` (default): the sender's own session, memory and files.
- ``channel``: the conversation itself, as actor ``slack_channel:<conversation
  id>`` or ``feishu_group:<chat_id>``, with its own namespace, session, memory
  and files, as Telegram groups are served (telegram_group.py). The sender
  reaches the agent only as a context line with their display name; no
  platform ID or phone number is in it.

In ``channel`` mode a Slack conversation other than a 1:1 DM (a public or
private channel, or a group DM) and a Feishu chat other than a p2p chat is
answered only when the operator has allowlisted it
(``ALLOW#slack_channel:<id>`` or ``ALLOW#feishu_group:<chat_id>``, written with
scripts/manage-allowlist.sh). These helpers are pure (no AWS or network calls)
so the rules can be tested on their own.
"""

import re

SLACK = "slack_channel"
FEISHU = "feishu_group"

# Slack conversation IDs: C... (public and private channels) and G... (older
# private channels and group DMs). D... is a 1:1 DM, which stays personal.
SLACK_CONVERSATION_ID_RE = re.compile(r"[CG][A-Z0-9]{2,31}")
# Feishu chat IDs are oc_ followed by 32 hex digits. The length cap keeps the
# namespace (feishu_group_<chat_id>) inside the bridge's 65-character limit
# (bridge/scoped-credentials.js).
FEISHU_CHAT_ID_RE = re.compile(r"oc_[A-Za-z0-9]{1,49}")

# Characters kept from a display name, as for Telegram groups: letters and
# digits in any script, space and . ' -. Brackets, colons, @ and line breaks
# are dropped so a name cannot pose as a header or another identity.
_NAME_DROP_RE = re.compile(r"[^\w .'-]|_")
MAX_SENDER_NAME = 48
# A name with more digits than this is more likely a phone number or an ID
# than a name, and is not used.
MAX_NAME_DIGITS = 6
UNNAMED_SENDER = "a member"


def sender_name(raw):
    """A display name reduced to safe characters, or "" when it cannot be used."""
    if not isinstance(raw, str):
        return ""
    name = " ".join(_NAME_DROP_RE.sub(" ", raw).split())[:MAX_SENDER_NAME].strip()
    if sum(character.isdigit() for character in name) > MAX_NAME_DIGITS:
        return ""
    return name


def slack_profile_name(profile):
    """The name Slack shows for a member: the display name, else the real name."""
    if not isinstance(profile, dict):
        return ""
    for key in ("display_name", "real_name"):
        name = sender_name(profile.get(key))
        if name:
            return name
    return ""


def agent_text(conversation, name, text):
    """The text sent to the agent: one context line naming the sender, then the message.

    conversation is "Slack channel" or "Feishu group". The line carries the
    sender's display name only, never a platform user ID or phone number.
    """
    return f"{conversation} message from {sender_name(name) or UNNAMED_SENDER}:\n{text}"


def slack_sender_is_external(envelope, event):
    """True when the sender is a Slack Connect member from another organization.

    In a channel shared with other organizations (``is_ext_shared_channel`` on
    the event envelope) the sender's team (``user_team``, else ``team``) must be
    a team the app is installed in (``authorizations[].team_id``). An unknown
    sender team counts as external. A channel that is not shared with another
    organization has no external members.
    """
    if not isinstance(envelope, dict) or envelope.get("is_ext_shared_channel") is not True:
        return False
    authorizations = envelope.get("authorizations")
    home_teams = {
        authorization["team_id"] for authorization in authorizations
        if isinstance(authorization, dict) and isinstance(authorization.get("team_id"), str)
        and authorization["team_id"]
    } if isinstance(authorizations, list) else set()
    event = event if isinstance(event, dict) else {}
    sender_team = event.get("user_team") or event.get("team")
    return not (isinstance(sender_team, str) and sender_team in home_teams)
