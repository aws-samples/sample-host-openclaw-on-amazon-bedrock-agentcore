"""Telegram albums get one agent turn and one reply (telegram_album.py).

Telegram delivers an album as one update per item, close together, and the
Router handles each in its own invocation. These tests replay that with a fake
clock: the other items' updates run while the first item's invocation waits
(FakeClock.sleep), so every interleaving is deterministic. The album record
lives in FakeAlbumTable, which parses the store's update and condition
expressions, applies them, and rejects what DynamoDB or boto3 would reject:
unaliased attribute names (reserved words such as ttl), unused placeholders and
float values.
"""

import copy
import heapq
import json
import logging
import os
import re
import sys
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib  # noqa: E402

index = importlib.import_module("index")
import telegram_album  # noqa: E402

START = 1_800_000_000.0
CHAT_ID = 1001
MEDIA_GROUP_ID = "13579"
CAPTION = "compare these three receipts"
USER_ID = "user_abc123"
# An opaque stored namespace (scripts/namespace_cutover.py), so the tests also show
# that album images go under route.namespace, not a prefix derived from the actor.
NAMESPACE = "ns_" + "a2" * 13
TG_ROUTE = SimpleNamespace(platform_version="V2", user_id=USER_ID, actor_id="telegram:1001", namespace=NAMESPACE)
FAILED_IMAGE_REPLY = "Sorry, I couldn't download that image. Please try again."


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """time.time() and time.sleep() on a virtual clock.

    sleep() advances the clock and, on the way, runs each callback scheduled
    with at(), in time order: the other invocations of an album run while the
    leader waits.
    """

    def __init__(self, start=START):
        self.start = start
        self.now = start
        self._events = []
        self._seq = 0

    def time(self):
        return self.now

    def at(self, offset, callback):
        """Run callback when the clock reaches start + offset seconds."""
        self._seq += 1
        heapq.heappush(self._events, (self.start + offset, self._seq, callback))

    def _run_until(self, target):
        while self._events and (target is None or self._events[0][0] <= target):
            when, _, callback = heapq.heappop(self._events)
            self.now = max(self.now, when)
            callback()

    def sleep(self, seconds):
        target = self.now + seconds
        self._run_until(target)
        self.now = max(self.now, target)

    def run_remaining(self):
        """Run the callbacks still scheduled: updates that arrive after the leader is done."""
        self._run_until(None)

    @property
    def elapsed(self):
        return self.now - self.start


class ConditionalCheckFailed(Exception):
    """What boto3 raises for a failed ConditionExpression (botocore is mocked here)."""

    def __init__(self):
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ServiceError(Exception):
    def __init__(self, code="InternalServerError"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


_TOKEN_RE = re.compile(r"\s*(#\w+|:\w+|\w+|[(),=])")


def _tokens(expression):
    tokens, position, expression = [], 0, expression.strip()
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if not match:
            raise AssertionError(f"cannot read {expression!r} at {position}")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def _check_value(value):
    if isinstance(value, float):
        raise AssertionError("boto3 refuses float values; use int or Decimal")
    if isinstance(value, dict):
        for inner in value.values():
            _check_value(inner)


def _stored(value):
    """Numbers come back from the boto3 resource as Decimal."""
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, dict):
        return {name: _stored(inner) for name, inner in value.items()}
    return value


class _Expression:
    """One expression's tokens, and the placeholders the request has used so far."""

    def __init__(self, text, names, values, used):
        self.tokens, self.position = _tokens(text), 0
        self.names, self.values, self.used = names, values, used

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, expected=None):
        if self.position >= len(self.tokens):
            raise AssertionError(f"expression ended, expected {expected!r}")
        token = self.tokens[self.position]
        self.position += 1
        if expected is not None and token != expected:
            raise AssertionError(f"expected {expected!r}, got {token!r}")
        return token

    def name(self):
        token = self.take()
        if not token.startswith("#"):
            raise AssertionError(f"attribute name {token!r} is not aliased")
        self.used.add(token)
        return self.names[token]

    def value(self):
        token = self.take()
        if not token.startswith(":"):
            raise AssertionError(f"expected a value placeholder, got {token!r}")
        self.used.add(token)
        return self.values[token]

    def finish(self):
        if self.position != len(self.tokens):
            raise AssertionError(f"unexpected tokens {self.tokens[self.position:]}")

    # Conditions: OR of ANDs of (...), NOT x, attribute_[not_]exists(#a), #a = :v.
    # Every operand is evaluated, so each placeholder counts as used.
    def condition(self, item):
        result = self._conjunction(item)
        while self.peek() == "OR":
            self.take()
            right = self._conjunction(item)
            result = result or right
        return result

    def _conjunction(self, item):
        result = self._factor(item)
        while self.peek() == "AND":
            self.take()
            right = self._factor(item)
            result = result and right
        return result

    def _factor(self, item):
        token = self.peek()
        if token == "(":
            self.take("(")
            result = self.condition(item)
            self.take(")")
            return result
        if token == "NOT":
            self.take()
            return not self._factor(item)
        if token in ("attribute_exists", "attribute_not_exists"):
            self.take()
            self.take("(")
            name = self.name()
            self.take(")")
            return (name in item) == (token == "attribute_exists")
        name = self.name()
        self.take("=")
        expected = _stored(self.value())
        return name in item and item[name] == expected

    # Updates: SET #a = :v | if_not_exists(#b, :v), ... and ADD #n :v, ...
    def update(self, item):
        new, updated = copy.deepcopy(item), []
        while self.peek() is not None:
            clause = self.take()
            if clause not in ("SET", "ADD"):
                raise AssertionError(f"unsupported clause {clause!r}")
            while True:
                name = self.name()
                if name in updated:
                    raise AssertionError(f"two document paths overlap: {name}")
                if clause == "ADD":
                    new[name] = new.get(name, Decimal(0)) + _stored(self.value())
                else:
                    self.take("=")
                    if self.peek() == "if_not_exists":
                        self.take()
                        self.take("(")
                        existing = self.name()
                        self.take(",")
                        default = _stored(self.value())
                        self.take(")")
                        new[name] = copy.deepcopy(item[existing]) if existing in item else default
                    else:
                        new[name] = _stored(self.value())
                updated.append(name)
                if self.peek() != ",":
                    break
                self.take(",")
        return new, updated


class FakeAlbumTable:
    """The identity table as the album store uses it: UpdateItem and GetItem."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self._failures = []

    def fail(self, operation, error, when=lambda request: True, times=1):
        """Raise error on the next `times` calls of operation for which when(request) holds (None: always)."""
        self._failures.append([operation, when, error, times])

    def _maybe_fail(self, operation, request):
        for failure in self._failures:
            name, when, error, times = failure
            if name == operation and times != 0 and when(request):
                if times is not None:
                    failure[3] = times - 1
                raise error

    def update_item(self, **request):
        self.calls.append(("update_item", request))
        self._maybe_fail("update_item", request)
        names = request.get("ExpressionAttributeNames", {})
        values = request.get("ExpressionAttributeValues", {})
        for value in values.values():
            _check_value(value)
        key = (request["Key"]["PK"], request["Key"]["SK"])
        item = self.items.get(key, {})
        used = set()
        allowed = True
        if "ConditionExpression" in request:
            condition = _Expression(request["ConditionExpression"], names, values, used)
            allowed = condition.condition(item)
            condition.finish()
        expression = _Expression(request["UpdateExpression"], names, values, used)
        new, updated = expression.update(item)
        expression.finish()
        if used != set(names) | set(values):
            raise AssertionError(f"unused placeholders: {sorted((set(names) | set(values)) - used)}")
        if not allowed:
            raise ConditionalCheckFailed()
        new.update(request["Key"])
        self.items[key] = new
        returns = request.get("ReturnValues", "NONE")
        if returns == "UPDATED_NEW":
            return {"Attributes": {name: copy.deepcopy(new[name]) for name in updated}}
        if returns == "ALL_NEW":
            return {"Attributes": copy.deepcopy(new)}
        if returns != "NONE":
            raise AssertionError(f"unexpected ReturnValues {returns!r}")
        return {}

    def get_item(self, **request):
        self.calls.append(("get_item", request))
        self._maybe_fail("get_item", request)
        if request.get("ConsistentRead") is not True:
            raise AssertionError("album reads must be consistent")
        item = self.items.get((request["Key"]["PK"], request["Key"]["SK"]))
        if item is None:
            return {}
        if "ProjectionExpression" in request:
            names = request["ExpressionAttributeNames"]
            tokens = [token.strip() for token in request["ProjectionExpression"].split(",")]
            if set(tokens) != set(names) or not all(token.startswith("#") for token in tokens):
                raise AssertionError("projection placeholders must match ExpressionAttributeNames")
            item = {names[token]: item[names[token]] for token in tokens if names[token] in item}
        return {"Item": copy.deepcopy(item)}


def _is_register(request):
    return "ADD" in request["UpdateExpression"]


def _is_close(request):
    return request["UpdateExpression"] == "SET #closed = :now"


# ---------------------------------------------------------------------------
# Telegram updates
# ---------------------------------------------------------------------------

def photo(message_id):
    return {"photo": [{"file_id": f"small{message_id}", "width": 90, "height": 90},
                      {"file_id": f"photo{message_id}", "width": 1280, "height": 960}]}


def update(message_id, caption=None, media=None, media_group_id=MEDIA_GROUP_ID,
           chat_id=CHAT_ID, chat_type="private"):
    message = {"message_id": message_id, "date": int(START),
               "chat": {"id": chat_id, "type": chat_type}, "from": {"id": 1001, "first_name": "A"}}
    message.update(media if media is not None else photo(message_id))
    if media_group_id is not None:
        message["media_group_id"] = media_group_id
    if caption is not None:
        message["caption"] = caption
    return json.dumps({"update_id": 900 + message_id, "message": message})


def image(message_id, namespace=NAMESPACE):
    """The image reference the patched upload returns for an update's photo."""
    return {"s3Key": f"{namespace}/_uploads/img_photo{message_id}.jpeg", "contentType": "image/jpeg"}


class AlbumTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeAlbumTable()
        self.clock = FakeClock()
        self.failing = set()  # file IDs whose download fails
        self.addCleanup(patch.stopall)
        patch.object(index, "identity_table", self.table).start()
        patch.object(telegram_album, "time", self.clock).start()
        patch.object(index, "_get_telegram_token", return_value="123:abc").start()
        patch.object(index, "resolve_user", return_value=(USER_ID, False)).start()
        self.route = patch.object(index, "get_or_create_session", return_value=TG_ROUTE).start()
        patch.object(index, "authorize_channel_actor").start()
        patch.object(index, "get_model_override", return_value=None).start()
        patch.object(index, "send_telegram_typing").start()
        patch.object(index, "_periodic_typing").start()
        patch.object(index, "_download_telegram_image", side_effect=self._download).start()
        patch.object(index, "_upload_image_to_s3", side_effect=self._upload).start()
        self.send = patch.object(index, "send_telegram_message").start()
        self.chunks = patch.object(index, "_send_telegram_chunks").start()
        self.invoke = patch.object(index, "invoke_agent_runtime", return_value={"response": "agent reply"}).start()

    def _download(self, message, token):
        file_id = message["photo"][-1]["file_id"]
        if file_id in self.failing:
            return None, None, None
        return file_id.encode(), "image/jpeg", "file.jpg"

    def _upload(self, image_bytes, namespace, content_type):
        return f"{namespace}/_uploads/img_{image_bytes.decode()}.jpeg"

    def deliver(self, first, *later):
        """Handle the first update now and each (offset, update) at its offset in seconds.

        Later updates whose offset falls inside the first invocation's wait run during
        it, as concurrent invocations do; the rest run after it returns.
        """
        for offset, body in later:
            self.clock.at(offset, lambda body=body: index.handle_telegram(body))
        index.handle_telegram(first)
        self.clock.run_remaining()

    def agent_messages(self):
        return [call.args[4] for call in self.invoke.call_args_list]

    def replies(self):
        return self.chunks.call_count + self.send.call_count


# ---------------------------------------------------------------------------
# Albums through handle_telegram
# ---------------------------------------------------------------------------

class TelegramAlbumTurns(AlbumTestCase):
    def test_three_photos_run_one_turn_with_the_caption(self):
        """Three photos, the caption on the first: one turn with every photo and the caption, one reply."""
        self.deliver(update(10, CAPTION), (0.1, update(11)), (0.2, update(12)))
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10), image(11), image(12)]}])
        self.assertEqual(self.chunks.call_count, 1)
        self.send.assert_not_called()
        # Closed once nothing had arrived for QUIET_SECONDS after the last item (0.2 s).
        self.assertGreaterEqual(self.clock.elapsed, 0.2 + telegram_album.QUIET_SECONDS)
        self.assertLess(self.clock.elapsed, 0.2 + telegram_album.QUIET_SECONDS + 0.5)

    def test_a_later_item_can_lead_and_images_stay_in_message_order(self):
        self.deliver(update(11), (0.1, update(10, CAPTION)), (0.2, update(12)))
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10), image(11), image(12)]}])
        self.assertEqual(self.replies(), 1)

    def test_a_redelivered_item_is_dropped(self):
        self.deliver(update(10, CAPTION), (0.1, update(11)), (0.15, update(11)), (0.2, update(12)),
                     (60.0, update(11)))
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10), image(11), image(12)]}])
        self.assertEqual(self.replies(), 1)

    def test_an_item_after_the_close_runs_on_its_own(self):
        self.deliver(update(10, CAPTION), (0.1, update(11)), (30.0, update(12)))
        self.assertEqual(self.agent_messages(), [
            {"text": CAPTION, "images": [image(10), image(11)]},
            {"text": "", "images": [image(12)]},  # as before albums were grouped
        ])
        self.assertEqual(self.chunks.call_count, 2)

    def test_captions_on_several_items_are_joined_in_order(self):
        self.deliver(update(10, "first"), (0.1, update(11, "second")), (0.2, update(12, "first")))
        self.assertEqual(self.agent_messages()[0]["text"], "first\n\nsecond")

    def test_an_album_without_a_caption_gets_the_default_text(self):
        self.deliver(update(10), (0.1, update(11)))
        self.assertEqual(self.agent_messages(),
                         [{"text": "The user sent 2 photos without a message.", "images": [image(10), image(11)]}])

    def test_one_failed_download_adds_a_note_and_no_extra_reply(self):
        self.failing = {"photo11"}
        self.deliver(update(10, CAPTION), (0.1, update(11)), (0.2, update(12)))
        self.assertEqual(self.agent_messages(), [{
            "text": f"{CAPTION}\n\n[1 photo could not be downloaded.]", "images": [image(10), image(12)],
        }])
        self.send.assert_not_called()
        self.assertEqual(self.chunks.call_count, 1)

    def test_the_leaders_failed_download_still_answers_the_album(self):
        self.failing = {"photo10"}
        self.deliver(update(10, CAPTION), (0.1, update(11)), (0.2, update(12)))
        self.assertEqual(self.agent_messages(), [{
            "text": f"{CAPTION}\n\n[1 photo could not be downloaded.]", "images": [image(11), image(12)],
        }])
        self.send.assert_not_called()

    def test_every_download_failed_sends_one_apology_and_no_turn(self):
        self.failing = {"photo10", "photo11", "photo12"}
        self.deliver(update(10, CAPTION), (0.1, update(11)), (0.2, update(12)))
        self.invoke.assert_not_called()
        self.assertEqual([call.args[1] for call in self.send.call_args_list], [telegram_album.ALL_FAILED_REPLY])

    def test_a_video_caption_reaches_the_agent_with_the_photos(self):
        video = {"video": {"file_id": "video10", "duration": 9, "mime_type": "video/mp4"}}
        self.deliver(update(10, "what do you see?", media=video), (0.1, update(11)), (0.2, update(12)))
        self.assertEqual(self.agent_messages(), [{
            "text": "what do you see?\n\n[The user also sent a video, which cannot be read yet.]",
            "images": [image(11), image(12)],
        }])
        self.assertEqual(self.replies(), 1)

    def test_ten_items_close_without_waiting_for_quiet(self):
        self.deliver(update(10, CAPTION), *[(0.05 * n, update(10 + n)) for n in range(1, 10)])
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10 + n) for n in range(10)]}])
        self.assertLess(self.clock.elapsed, telegram_album.QUIET_SECONDS)

    def test_a_register_error_handles_the_item_on_its_own(self):
        self.table.fail("update_item", ServiceError(), when=_is_register)
        self.deliver(update(10, CAPTION))
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10)]}])
        self.assertEqual(self.clock.elapsed, 0)  # no wait

    def test_a_single_photo_makes_no_album_call(self):
        self.deliver(update(10, CAPTION, media_group_id=None))
        self.assertEqual(self.table.calls, [])
        self.assertEqual(self.agent_messages(), [{"text": CAPTION, "images": [image(10)]}])
        self.assertEqual(self.clock.elapsed, 0)

    def test_a_group_album_is_unchanged(self):
        """Groups keep one turn per addressed item; nothing is grouped there."""
        group_id = -1001234567890
        group_route = SimpleNamespace(platform_version="V2", user_id="user_group1",
                                      actor_id=f"telegram_group:{group_id}", namespace=f"telegram_group_{group_id}")
        self.route.return_value = group_route
        with patch.object(index, "TELEGRAM_GROUPS_ENABLED", True), \
                patch.object(index, "_telegram_bot_username", return_value="TestBot"), \
                patch.object(index, "_is_telegram_group_allowed", return_value=True), \
                patch.object(index, "_is_disabled_telegram_sender", return_value=False):
            self.deliver(update(10, "@TestBot compare these", chat_id=group_id, chat_type="supergroup"),
                         (0.1, update(11, chat_id=group_id, chat_type="supergroup")),
                         (0.2, update(12, chat_id=group_id, chat_type="supergroup")))
        self.assertEqual(self.table.calls, [])
        self.assertEqual(self.invoke.call_count, 1)
        self.assertEqual(self.agent_messages()[0]["images"], [image(10, namespace=f"telegram_group_{group_id}")])

    def test_album_logs_carry_counts_only(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            self.deliver(update(10, CAPTION), (0.1, update(11)), (0.2, update(12)), (0.25, update(12)),
                         (30.0, update(13)))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        album_lines = [record.getMessage() for record in records if record.name == "telegram_album"]
        self.assertIn("Telegram: album items=3 spread_ms=200", album_lines)
        self.assertIn("Telegram: dropped a redelivered album item", album_lines)
        self.assertIn("Telegram: album item handled on its own (the album had closed)", album_lines)
        for line in album_lines:
            for identifier in (str(CHAT_ID), MEDIA_GROUP_ID, "m10", "m11", "m12", "m13", USER_ID):
                self.assertNotIn(identifier, line)
        self.assertFalse([record for record in records if MEDIA_GROUP_ID in record.getMessage()])


# ---------------------------------------------------------------------------
# The coordinator and the store, without handle_telegram
# ---------------------------------------------------------------------------

class AlbumCoordination(AlbumTestCase):
    def setUp(self):
        super().setUp()
        self.store = telegram_album.DynamoAlbumStore(self.table)
        self.key = telegram_album.record_key(CHAT_ID, MEDIA_GROUP_ID)

    def item(self, message_id, caption="", reply=None):
        return telegram_album.AlbumItem(self.store, self.key, message_id, USER_ID, caption=caption,
                                        reply=reply, clock=self.clock)

    @staticmethod
    def own(message_id, text=""):
        return {"text": text, "images": [image(message_id)]}

    def test_a_slow_item_is_waited_for(self):
        leader, slow = self.item(10, CAPTION), self.item(11)
        self.assertEqual(leader.join(), telegram_album.LEADER)
        self.clock.at(0.1, slow.join)
        self.clock.at(5.0, lambda: self.assertIsNone(slow.finish(self.own(11))))
        self.assertEqual(leader.finish(self.own(10, CAPTION)), {"text": CAPTION, "images": [image(10), image(11)]})
        self.assertEqual(slow.role, telegram_album.MEMBER)
        self.assertGreaterEqual(self.clock.elapsed, 5.0)
        self.assertLess(self.clock.elapsed, 5.5)

    def test_an_item_still_pending_at_the_deadline_runs_on_its_own(self):
        leader, slow = self.item(10, CAPTION), self.item(11)
        leader.join()
        self.clock.at(0.1, slow.join)
        self.assertEqual(leader.finish(self.own(10, CAPTION)), {"text": CAPTION, "images": [image(10)]})
        self.assertGreaterEqual(self.clock.elapsed, telegram_album.MAX_WAIT_SECONDS)
        self.assertLess(self.clock.elapsed, telegram_album.MAX_WAIT_SECONDS + 0.5)
        self.assertEqual(slow.finish(self.own(11)), self.own(11))  # the album closed without it

    def test_a_late_failed_item_sends_its_own_failure_reply(self):
        replies = []
        leader, slow = self.item(10, CAPTION), self.item(11, reply=replies.append)
        leader.join()
        self.clock.at(0.1, slow.join)
        leader.finish(self.own(10, CAPTION))
        slow.hold_reply(FAILED_IMAGE_REPLY)
        self.assertIsNone(slow.finish(None))
        self.assertEqual(replies, [FAILED_IMAGE_REPLY])

    def test_a_member_failure_reply_is_held(self):
        replies = []
        leader, member = self.item(10, CAPTION), self.item(11, reply=replies.append)
        leader.join()
        member.join()
        member.hold_reply(FAILED_IMAGE_REPLY)
        self.assertIsNone(member.finish(None))
        self.assertEqual(replies, [])

    def test_read_errors_keep_the_leader_waiting_until_the_deadline(self):
        self.table.fail("get_item", ServiceError("ProvisionedThroughputExceededException"), times=None)
        leader = self.item(10, CAPTION)
        leader.join()
        self.assertEqual(leader.finish(self.own(10, CAPTION)), {"text": CAPTION, "images": [image(10)]})
        self.assertGreaterEqual(self.clock.elapsed, telegram_album.MAX_WAIT_SECONDS)

    def test_a_failed_leader_write_does_not_stop_the_album(self):
        leader, member = self.item(10, CAPTION), self.item(11)
        leader.join()
        member.join()
        self.table.fail("update_item", ServiceError(), when=lambda request: request["UpdateExpression"] == "SET #item = :entry")
        member_message = self.own(11)
        self.clock.at(0.1, lambda: member.finish(member_message))
        self.assertEqual(leader.finish(self.own(10, CAPTION)), {"text": CAPTION, "images": [image(10), image(11)]})

    def test_a_close_error_answers_the_leaders_own_item(self):
        self.table.fail("update_item", ServiceError(), when=_is_close)
        leader = self.item(10, CAPTION)
        leader.join()
        self.assertEqual(leader.finish(self.own(10, CAPTION)), self.own(10, CAPTION))

    def test_store_register_settle_and_close(self):
        now_ms = int(START * 1000)
        self.assertEqual(self.store.register(self.key, "m10", USER_ID, now_ms), 1)
        self.assertEqual(self.store.register(self.key, "m11", USER_ID, now_ms + 100), 2)
        self.assertIsNone(self.store.register(self.key, "m11", USER_ID, now_ms + 150))  # already registered
        self.assertIsNone(self.store.register(self.key, "m12", "user_other", now_ms + 160))  # another user
        self.assertTrue(self.store.contains(self.key, "m11"))
        self.assertFalse(self.store.contains(self.key, "m12"))
        self.assertTrue(self.store.settle(self.key, "m10", {"s": "ready", "k": "a", "t": "image/jpeg"}))
        record = self.store.read(self.key)
        self.assertEqual((record["firstAt"], record["lastAt"], record["itemCount"]), (now_ms, now_ms + 100, 2))
        self.assertEqual(record["ttl"], int(START) + telegram_album.RECORD_TTL_SECONDS)
        self.assertEqual((record["m10"]["s"], record["m11"]["s"], record["userId"]), ("ready", "pending", USER_ID))
        closed = self.store.close(self.key, now_ms + 2500)
        self.assertEqual(closed["closed"], now_ms + 2500)
        self.assertEqual(telegram_album.album_entries(closed).keys(), {10, 11})
        self.assertIsNone(self.store.close(self.key, now_ms + 2600))
        self.assertIsNone(self.store.register(self.key, "m13", USER_ID, now_ms + 2700))
        self.assertFalse(self.store.settle(self.key, "m11", {"s": "ready", "k": "b", "t": "image/jpeg"}))

    def test_store_never_creates_a_record_without_a_registration(self):
        other = telegram_album.record_key(CHAT_ID, "24680")
        self.assertFalse(self.store.settle(other, "m1", {"s": "ready", "k": "a", "t": "image/jpeg"}))
        self.assertIsNone(self.store.close(other, int(START * 1000)))
        self.assertEqual(self.store.read(other), {})
        self.assertNotIn((other["PK"], other["SK"]), self.table.items)


class AlbumMessage(unittest.TestCase):
    def test_texts_and_notes(self):
        ready = {"s": "ready", "k": "ns/_uploads/a.jpeg", "t": "image/jpeg"}
        cases = [
            ([ready, ready], {"text": "The user sent 2 photos without a message.", "images": [image_of(ready)] * 2}),
            ([{**ready, "c": "one"}, {"s": "failed"}, {"s": "failed", "c": "two"}],
             {"text": "one\n\ntwo\n\n[2 photos could not be downloaded.]", "images": [image_of(ready)]}),
            ([ready, {"s": "unsupported", "kind": "video"}, {"s": "unsupported", "kind": "audio"}],
             {"text": "The user sent 1 photo without a message.\n\n"
                      "[The user also sent a video and an audio file, which cannot be read yet.]",
              "images": [image_of(ready)]}),
            ([{"s": "unsupported", "kind": "video", "c": "look"}],
             "look\n\n[The user also sent a video, which cannot be read yet. Only the text above was received.]"),
            ([ready, {"s": "unsupported", "kind": "video"}, {"s": "unsupported", "kind": "video"}],
             {"text": "The user sent 1 photo without a message.\n\n"
                      "[The user also sent 2 videos, which cannot be read yet.]",
              "images": [image_of(ready)]}),
            ([{**ready, "c": "  "}], {"text": "The user sent 1 photo without a message.", "images": [image_of(ready)]}),
        ]
        for items, expected in cases:
            with self.subTest(items=items):
                self.assertEqual(telegram_album.album_message(items), (expected, None))

    def test_nothing_to_send(self):
        self.assertEqual(telegram_album.album_message([{"s": "failed"}, {"s": "failed", "c": "hi"}]),
                         (None, telegram_album.ALL_FAILED_REPLY))
        self.assertEqual(telegram_album.album_message([{"s": "unsupported", "kind": "video"}]), (None, None))
        # Only items the bot cannot read and no caption: the notice instead of a turn.
        self.assertEqual(telegram_album.album_message([{"s": "unsupported", "kind": "video"}] * 2, notice="N"),
                         (None, "N"))
        self.assertEqual(telegram_album.album_message([], notice="N"), (None, None))

    def test_join_ignores_updates_that_are_not_album_items(self):
        store = MagicMock()
        bad = [{"message_id": 1, "chat": {"id": 1}},  # no media_group_id
               {"message_id": 1, "chat": {"id": 1}, "media_group_id": 13579},  # not a string
               {"message_id": 1, "chat": {"id": 1}, "media_group_id": "a:b"},
               {"message_id": True, "chat": {"id": 1}, "media_group_id": "1"},
               {"message_id": 0, "chat": {"id": 1}, "media_group_id": "1"},
               {"message_id": 1, "chat": {"id": "1"}, "media_group_id": "1"}]
        for message in bad:
            with self.subTest(message=message):
                self.assertIsNone(telegram_album.join(message, "", USER_ID, True, store, None))
        store.assert_not_called()
        self.assertEqual(store.method_calls, [])


def image_of(entry):
    return {"s3Key": entry["k"], "contentType": entry["t"]}


if __name__ == "__main__":
    unittest.main()
