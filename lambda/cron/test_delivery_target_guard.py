"""
Tests for the Cron Lambda's delivery-target check in verify_cron_event.

On Telegram (as on WhatsApp, LINE and Discord) a schedule whose delivery
target is not the actor's own channel user id does not run: the owner gets
one notice in their own chat instead, once per schedule (cron_target_check
"refuse", the default). With cron_target_check "log" such a schedule is
logged for operator visibility and still delivered, as Slack and Feishu
targets always are (see test_schedule_delivery_checks.py).

All IDs below are synthetic placeholders.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

index = importlib.import_module("index")

ACTOR = "telegram:123456"
USER = "user_abc"
SCHED = "0a1b2c3d"
NOTICE = ("[Scheduled: Brief] This scheduled task is paused because it was set to deliver to a "
          "different chat. Create it again from the chat that should receive it.")


class FakeClientError(Exception):
    def __init__(self, code="ConditionalCheckFailedException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, items):
        self.items = dict(items)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression,  # noqa: N803
                    ExpressionAttributeNames, ExpressionAttributeValues):
        # Only the target notice claim reaches the table from verify_cron_event.
        assert Key == {"PK": f"SCHED#{USER}", "SK": f"FIRE#{SCHED}"}
        assert UpdateExpression == "SET #notice = :at REMOVE #ttl"
        assert ConditionExpression == "attribute_not_exists(#notice)"
        assert ExpressionAttributeNames == {"#notice": "targetNoticeAt", "#ttl": "ttl"}
        item = self.items.setdefault((Key["PK"], Key["SK"]), {"PK": Key["PK"], "SK": Key["SK"]})
        if "targetNoticeAt" in item:
            raise FakeClientError()
        item["targetNoticeAt"] = ExpressionAttributeValues[":at"]
        item.pop("ttl", None)
        return {}


def _table(channel_target):
    return FakeTable({
        (f"CHANNEL#{ACTOR}", "PROFILE"): {"PK": f"CHANNEL#{ACTOR}", "SK": "PROFILE", "userId": USER},
        (f"ACTOR#{ACTOR}", "OWNER"): {
            "PK": f"ACTOR#{ACTOR}", "SK": "OWNER", "userId": USER, "actorId": ACTOR,
            "platformVersion": "V2", "ownerGeneration": 1, "runtimeActorId": ACTOR,
        },
        (f"USER#{USER}", "PROFILE"): {"PK": f"USER#{USER}", "SK": "PROFILE"},
        (f"USER#{USER}", f"CRON#{SCHED}"): {
            "PK": f"USER#{USER}", "SK": f"CRON#{SCHED}", "scheduleId": SCHED,
            "channelActorId": ACTOR, "actorId": ACTOR, "channel": "telegram",
            "channelTarget": channel_target, "message": "brief", "scheduleName": "Brief",
        },
    })


def _event(channel_target):
    return {
        "userId": USER, "channelActorId": ACTOR, "actorId": ACTOR, "channel": "telegram",
        "channelTarget": channel_target, "message": "brief", "scheduleId": SCHED, "scheduleName": "Brief",
    }


class TestDeliveryTargetGuard(unittest.TestCase):
    def setUp(self):
        self.deliver = MagicMock()
        for target, attr in (("get_or_create_session", lambda uid: SimpleNamespace(actor_id=ACTOR)),
                             ("authorize_channel_actor", MagicMock()),
                             ("deliver_response", self.deliver),
                             ("CRON_TARGET_CHECK", "refuse"),
                             ("CRON_LEGACY_RECORDS", "read")):
            p = patch.object(index, target, attr)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(index, "ClientError", FakeClientError)
        p.start()
        self.addCleanup(p.stop)

    def _verify(self, channel_target, table=None):
        with patch.object(index, "identity_table", table or _table(channel_target)), \
                self.assertLogs(index.logger, level="INFO") as logs:
            job, error = index.verify_cron_event(_event(channel_target))
        return job, error, "\n".join(logs.output)

    def test_self_target_delivers_without_the_guard_warning(self):
        # The actor's own chat id (the suffix of telegram:123456).
        job, error, output = self._verify("123456")
        self.assertIsNone(error)
        self.assertIsNotNone(job)
        self.assertEqual(job["channelTarget"], "123456")
        self.assertNotIn("not the actor's own chat id", output)
        self.assertNotIn("refused", output)
        self.deliver.assert_not_called()

    def test_foreign_target_is_refused_with_one_notice_to_the_owner(self):
        # A target that is not the actor's own chat id: nothing runs, and the
        # owner is told once, in their own chat, never in the stored one.
        table = _table("999999")
        for _ in range(2):
            job, error, output = self._verify("999999", table)
            self.assertIsNone(job)
            self.assertEqual(error, {"statusCode": 403, "body": "Delivery target is not the owner's chat"})
            self.assertIn(f"Scheduled task {SCHED} refused: its delivery target is not the owner's chat", output)
            self.assertNotIn("999999", output)
        self.deliver.assert_called_once_with("telegram", "123456", NOTICE)
        self.assertIn("targetNoticeAt", table.items[(f"SCHED#{USER}", f"FIRE#{SCHED}")])

    def test_foreign_target_is_warned_but_still_delivered_in_log_mode(self):
        # cron_target_check "log": flagged, not refused; the record still
        # drives delivery (log, not block).
        with patch.object(index, "CRON_TARGET_CHECK", "log"):
            job, error, output = self._verify("999999")
        self.assertIsNone(error)
        self.assertIsNotNone(job)
        self.assertEqual(job["channelTarget"], "999999")
        self.assertIn("not the actor's own chat id", output)
        self.assertIn(SCHED, output)
        self.deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
