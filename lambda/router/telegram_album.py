"""One agent turn for a Telegram album (media group) in a private chat.

Telegram delivers an album, up to 10 photos and videos sent together, as one
update per item with a shared ``media_group_id``. The caption is on one item,
normally the first. The Router handles each update in its own invocation, so
without coordination every photo runs its own turn and gets its own reply, and
the caption reaches only one of them.

The items of an album meet in one identity-table record,
``DEDUP#telegram_album:<chat_id>:<media_group_id>`` / ``ALBUM``. Each item is a
top-level attribute ``m<message_id>`` (top-level, because ``SET map.key`` fails
while the map does not exist yet): ``pending`` while its image is stored, then
``ready`` (with the S3 key), ``failed`` or ``unsupported`` (a video or audio
file: the agent is told about it, and its caption still counts; an album of
only such items and no caption gets one notice). The first item to register
leads. Once its own item has settled, it polls the record and closes it when
no other item is pending and none has registered for QUIET_SECONDS, when
MAX_WAIT_SECONDS have passed since it registered, or when MAX_ITEMS have
settled. It then runs one turn with the ready images in message order and the
captions. The other invocations stop after storing their image. An item that
registers or settles after the close runs on its own, as before, so no photo
is lost; a redelivered update (same message_id) is dropped.

The store and the clock are passed in, so the coordination is tested without
AWS. Logs carry counts and durations only, never chat, message or album IDs.
"""

import logging
import re
import time

import unsupported_media

logger = logging.getLogger(__name__)

QUIET_SECONDS = 2.0
MAX_WAIT_SECONDS = 8.0
MAX_ITEMS = 10  # Telegram's limit for one album
POLL_SECONDS = 0.3
RECORD_TTL_SECONDS = 3600
MAX_CAPTION_CHARS = 1024  # Telegram's caption limit

# Item states in the record.
PENDING = "pending"
READY = "ready"
FAILED = "failed"
UNSUPPORTED = "unsupported"

# What join() made of an update.
LEADER = "leader"
MEMBER = "member"
DUPLICATE = "duplicate"
ALONE = "alone"

ALL_FAILED_REPLY = "Sorry, I couldn't download those photos. Please try again."

# Telegram sends media_group_id as a short decimal string; a value that does not
# look like an ID is not grouped.
MEDIA_GROUP_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")
_ITEM_ATTRIBUTE_RE = re.compile(r"m([0-9]{1,19})")
_UNSUPPORTED_KINDS = ("video", "audio")


def record_key(chat_id, media_group_id):
    return {"PK": f"DEDUP#telegram_album:{chat_id}:{media_group_id}", "SK": "ALBUM"}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value):
    """A stored number (Decimal from DynamoDB) as an int; 0 when absent."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _condition_failed(error):
    response = getattr(error, "response", None)
    return isinstance(response, dict) and (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


class DynamoAlbumStore:
    """Album records in the identity table (a boto3 Table resource).

    Times are epoch milliseconds (boto3 refuses floats); ``ttl`` is in seconds.
    """

    def __init__(self, table):
        self._table = table

    def register(self, key, attribute, user_id, now_ms):
        """Add a pending item. Returns the album's item count, or None when the album
        is closed, already has this item, or belongs to another user."""
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #item = :pending, #last = :now, #first = if_not_exists(#first, :now), "
                    "#user = if_not_exists(#user, :user), #ttl = :ttl ADD #count :one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(#closed) AND attribute_not_exists(#item) "
                    "AND (attribute_not_exists(#user) OR #user = :user)"
                ),
                ExpressionAttributeNames={
                    "#item": attribute, "#last": "lastAt", "#first": "firstAt", "#user": "userId",
                    "#ttl": "ttl", "#count": "itemCount", "#closed": "closed",
                },
                ExpressionAttributeValues={
                    ":pending": {"s": PENDING}, ":now": now_ms, ":user": user_id,
                    ":ttl": now_ms // 1000 + RECORD_TTL_SECONDS, ":one": 1,
                },
                ReturnValues="UPDATED_NEW",
            )
        except Exception as error:
            if _condition_failed(error):
                return None
            raise
        return int(response["Attributes"]["itemCount"])

    def contains(self, key, attribute):
        item = self._table.get_item(
            Key=key, ConsistentRead=True, ProjectionExpression="#item",
            ExpressionAttributeNames={"#item": attribute},
        ).get("Item") or {}
        return attribute in item

    def settle(self, key, attribute, entry):
        """Replace a registered item with its outcome. False when the album closed first."""
        try:
            self._table.update_item(
                Key=key,
                UpdateExpression="SET #item = :entry",
                ConditionExpression="attribute_exists(#item) AND attribute_not_exists(#closed)",
                ExpressionAttributeNames={"#item": attribute, "#closed": "closed"},
                ExpressionAttributeValues={":entry": entry},
            )
        except Exception as error:
            if _condition_failed(error):
                return False
            raise
        return True

    def read(self, key):
        return self._table.get_item(Key=key, ConsistentRead=True).get("Item") or {}

    def close(self, key, now_ms):
        """Close the album. Returns the closed record, or None if it was closed already."""
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression="SET #closed = :now",
                ConditionExpression="attribute_exists(#count) AND attribute_not_exists(#closed)",
                ExpressionAttributeNames={"#closed": "closed", "#count": "itemCount"},
                ExpressionAttributeValues={":now": now_ms},
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if _condition_failed(error):
                return None
            raise
        return response.get("Attributes") or {}


def join(message, caption, user_id, is_image, store, reply, clock=None, notice=None):
    """Register a private-chat photo, video or audio update in its album.

    Returns None when the update is not part of an album, or when it must be
    handled on its own (the album already closed, or the store failed). Otherwise
    returns the AlbumItem: drop the update if ``duplicate`` is set, else pass the
    item's own agent message to ``finish``. Documents are not grouped; the caller
    does not pass them. notice is the reply for an album whose items are all
    videos or audio files without a caption.
    """
    media_group_id = message.get("media_group_id")
    message_id = message.get("message_id")
    chat_id = (message.get("chat") or {}).get("id")
    if (not isinstance(media_group_id, str) or not MEDIA_GROUP_ID_RE.fullmatch(media_group_id)
            or not _is_int(message_id) or message_id <= 0 or not _is_int(chat_id)):
        return None
    kind = "photo" if is_image else next((name for name in _UNSUPPORTED_KINDS if message.get(name)), "media")
    item = AlbumItem(store, record_key(chat_id, media_group_id), message_id, user_id,
                     caption=caption, kind=kind, reply=reply, clock=clock, notice=notice)
    return None if item.join() == ALONE else item


class AlbumItem:
    """One update's part in an album: join(), then finish() with its agent message."""

    def __init__(self, store, key, message_id, user_id, caption="", kind="photo", reply=None, clock=None,
                 notice=None):
        self.store = store
        self.key = key
        self.message_id = message_id
        self.attribute = f"m{message_id}"
        self.user_id = user_id
        self.caption = (caption or "")[:MAX_CAPTION_CHARS]
        self.kind = kind
        self.clock = clock or time  # anything with time() and sleep()
        self.notice = notice
        self.role = None
        self._reply = reply or (lambda _text: None)
        self._held_reply = None
        self._joined_ms = None
        self._entry = None

    @property
    def duplicate(self):
        return self.role == DUPLICATE

    def _now_ms(self):
        return round(self.clock.time() * 1000)

    def join(self):
        """Register this item. Returns LEADER, MEMBER, DUPLICATE or ALONE."""
        self._joined_ms = self._now_ms()
        try:
            count = self.store.register(self.key, self.attribute, self.user_id, self._joined_ms)
        except Exception as error:  # noqa: BLE001 -- handled on its own, as before albums were grouped
            logger.warning("Telegram: album registration failed: %s", type(error).__name__)
            self.role = ALONE
            return self.role
        if count is not None:
            self.role = LEADER if count == 1 else MEMBER
            return self.role
        try:
            already_registered = self.store.contains(self.key, self.attribute)
        except Exception as error:  # noqa: BLE001 -- a possible redelivery then runs, as before
            logger.warning("Telegram: album lookup failed: %s", type(error).__name__)
            already_registered = False
        if already_registered:
            logger.info("Telegram: dropped a redelivered album item")
            self.role = DUPLICATE
        else:
            logger.info("Telegram: album item handled on its own (the album had closed)")
            self.role = ALONE
        return self.role

    def hold_reply(self, text):
        """Stand-in for the item's own failure reply: sent only if the item ends up alone."""
        self._held_reply = text

    def finish(self, message):
        """Store this item's outcome; return the agent message to send now, or None.

        ``message`` is the item's own agent message: a dict with one image, None when
        its image could not be stored (its failure reply was held), or the caption of
        a video or audio file. The leader returns the album's message, or None after
        replying itself; another member returns None. An item the album closed without
        gets back its own message, as if it had arrived alone.
        """
        self._entry = self._entry_for(message)
        try:
            stored = self.store.settle(self.key, self.attribute, self._entry)
        except Exception as error:  # noqa: BLE001 -- a member then runs on its own; the leader carries on
            logger.warning("Telegram: album item could not be stored: %s", type(error).__name__)
            stored = None
        if self.role == MEMBER:
            return None if stored else self._alone(message)
        # The leader keeps its own outcome in memory, so a failed write does not stop it.
        if stored is False:
            return self._alone(message)
        record = self._collect()
        if record is None:
            return self._alone(message)
        return self._album_turn(record)

    def _entry_for(self, message):
        if self.kind != "photo":
            entry = {"s": UNSUPPORTED, "kind": self.kind}
        elif isinstance(message, dict) and message.get("images"):
            image = message["images"][0]
            entry = {"s": READY, "k": image["s3Key"], "t": image["contentType"]}
        else:
            entry = {"s": FAILED}
        if self.caption:
            entry["c"] = self.caption
        return entry

    def _alone(self, message):
        logger.info("Telegram: album item handled on its own")
        if message is None and self._held_reply:
            self._reply(self._held_reply)
        return message or None

    def _collect(self):
        """Wait until the album settles (or the deadline), then close it.

        Returns the closed record, or None when it could not be closed.
        """
        deadline_ms = self._joined_ms + int(MAX_WAIT_SECONDS * 1000)
        while True:
            now_ms = self._now_ms()
            if now_ms >= deadline_ms:
                break
            try:
                record = self.store.read(self.key)
            except Exception as error:  # noqa: BLE001 -- keep waiting; the deadline still closes it
                logger.warning("Telegram: album read failed: %s", type(error).__name__)
                record = None
            if record and self._settled(record, now_ms):
                break
            self.clock.sleep(POLL_SECONDS)
        try:
            closed = self.store.close(self.key, self._now_ms())
        except Exception as error:  # noqa: BLE001 -- the leader then answers its own item
            logger.error("Telegram: album could not be closed: %s", type(error).__name__)
            return None
        if closed is None:
            logger.warning("Telegram: album was closed by another invocation")
        return closed

    def _settled(self, record, now_ms):
        entries = album_entries(record)
        if any(entry.get("s") == PENDING for message_id, entry in entries.items() if message_id != self.message_id):
            return False
        return len(entries) >= MAX_ITEMS or now_ms - _number(record.get("lastAt")) >= int(QUIET_SECONDS * 1000)

    def _album_turn(self, record):
        entries = album_entries(record)
        entries[self.message_id] = self._entry
        # Items still pending at the close run on their own when they settle.
        items = [entries[message_id] for message_id in sorted(entries) if entries[message_id].get("s") != PENDING]
        spread_ms = max(0, _number(record.get("lastAt")) - _number(record.get("firstAt")))
        logger.info("Telegram: album items=%d spread_ms=%d", len(items), spread_ms)
        message, reply_text = album_message(items, self.notice)
        if reply_text:
            self._reply(reply_text)
        return message


def album_entries(record):
    """The items of an album record as {message_id: entry}."""
    entries = {}
    for name, value in record.items():
        match = _ITEM_ATTRIBUTE_RE.fullmatch(name)
        if match and isinstance(value, dict):
            entries[int(match.group(1))] = value
    return entries


def _photos(count):
    return "1 photo" if count == 1 else f"{count} photos"


_KIND_NAMES = {"video": ("a video", "videos"), "audio": ("an audio file", "audio files")}
_OTHER_NAMES = ("an attachment", "attachments")


def _unsupported_names(items):
    """Name the album's items the bot cannot read: "a video", "2 videos and an audio file"."""
    counts = {}
    for item in items:
        if item.get("s") == UNSUPPORTED:
            kind = item.get("kind") or "media"
            counts[kind] = counts.get(kind, 0) + 1
    names = []
    for kind, count in counts.items():
        one, many = _KIND_NAMES.get(kind, _OTHER_NAMES)
        names.append(one if count == 1 else f"{count} {many}")
    return " and ".join(names)


def album_message(items, notice=None):
    """The agent message for an album's settled items, given in message order.

    Returns (message, reply). message is for the agent: a dict with the ready
    images, or the captions alone when no image is ready. reply is a text for the
    user instead of a turn: an apology when every download failed, or notice when
    every item is one the bot cannot read and none has a caption. Both are None
    when there is nothing to answer.
    """
    images = [{"s3Key": item["k"], "contentType": item["t"]} for item in items if item.get("s") == READY]
    failed = sum(1 for item in items if item.get("s") == FAILED)
    captions = list(dict.fromkeys(item["c"] for item in items if (item.get("c") or "").strip()))
    unsupported = _unsupported_names(items)
    if not images and failed:
        return None, ALL_FAILED_REPLY
    if not images and not captions:
        return None, (notice if unsupported else None)
    text = "\n\n".join(captions) or f"The user sent {_photos(len(images))} without a message."
    if failed:
        text += f"\n\n[{_photos(failed)} could not be downloaded.]"
    if unsupported:
        text += unsupported_media.note(unsupported, text_only=not images)
    return ({"text": text, "images": images} if images else text), None
