"""
Tests for the Cron Lambda's defence-in-depth delivery-target check in
verify_cron_event: a schedule whose delivery target is not the actor's own
channel user id is logged (for operator visibility) but still delivered, so
legitimate Slack/Feishu/group targets are not broken.

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


class FakeClientError(Exception):
    pass


class FakeTable:
    def __init__(self, items):
        self.items = dict(items)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


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
        for target, attr in (("get_or_create_session", lambda uid: SimpleNamespace(actor_id=ACTOR)),
                             ("authorize_channel_actor", MagicMock())):
            p = patch.object(index, target, attr)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(index, "ClientError", FakeClientError)
        p.start()
        self.addCleanup(p.stop)

    def _verify(self, channel_target):
        with patch.object(index, "identity_table", _table(channel_target)), \
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

    def test_foreign_target_is_warned_but_still_delivered(self):
        # A target that is not the actor's own chat id is flagged, not refused:
        # the record still drives delivery (log, not block).
        job, error, output = self._verify("999999")
        self.assertIsNone(error)
        self.assertIsNotNone(job)
        self.assertEqual(job["channelTarget"], "999999")
        self.assertIn("not the actor's own chat id", output)
        self.assertIn(SCHED, output)


if __name__ == "__main__":
    unittest.main()
