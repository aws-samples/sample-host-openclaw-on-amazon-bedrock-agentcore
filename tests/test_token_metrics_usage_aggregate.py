"""Token metrics Lambda: the per-user daily usage item in the identity table.

Every Bedrock call is also added, with one atomic UpdateItem ... ADD, to
USAGE#<internal user ID> / DAY#<UTC day> in the identity table, which the
Router and the Cron Lambda read for per_user_daily_cost_usd and /usage
(lambda/shared/python/usage_limits.py). Records of bridge images before the
user ID change name the actor instead; its user comes from
CHANNEL#<actor>/PROFILE, cached for 10 minutes. A failure to write the item
never blocks the token-usage row or the metrics.

Hermetic: FakeTable (tests/test_token_metrics_totals.py) applies the SET / ADD
subset of UpdateItem and refuses floats as boto3 does.

    pytest tests/test_token_metrics_usage_aggregate.py -v
"""

import hashlib
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.test_token_metrics_attribution import DAY, SESSION, USER, _cwl_event, _log, _meta, _user_meta
from tests.test_token_metrics_totals import FakeTable, _load_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda" / "shared" / "python"))
import usage_limits  # noqa: E402

LEGACY_USER = "user_aaaaaaaaaaaaaaaa"
# 1000 input and 50 output tokens of Opus 5.5 (global), the default in _log: 1000 * 4 / 1M + 50 * 20 / 1M.
ONE_CALL = Decimal("0.005")
# 00:00 UTC two days after DAY ends: 2026-10-19T00:00:00Z.
TTL = int(datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp())


class IdentityTable(FakeTable):
    """FakeTable plus the CHANNEL# GetItem the Lambda sends for an actor."""

    def __init__(self):
        super().__init__()
        self.gets = []
        self.get_error = None
        self.update_error = None

    def get_item(self, Key, ProjectionExpression=None):  # noqa: N803 (AWS kwargs)
        self.gets.append(Key)
        if self.get_error:
            raise self.get_error
        assert ProjectionExpression == "userId"
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        if self.update_error:
            raise self.update_error
        return super().update_item(**kwargs)


@pytest.fixture
def tm():
    mod = _load_module()
    mod.table = FakeTable()
    mod.identity_table = IdentityTable()
    mod.cloudwatch = MagicMock()
    mod._actor_users.clear()
    return mod


def _usage(tm, user=USER, day=DAY):
    return tm.identity_table.items.get((f"USAGE#{user}", f"DAY#{day}"))


def _link(tm, actor, user=LEGACY_USER):
    tm.identity_table.items[(f"CHANNEL#{actor}", "PROFILE")] = {
        "PK": f"CHANNEL#{actor}", "SK": "PROFILE", "userId": user}


def _throttle(operation):
    return ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, operation)


def test_one_call_is_added_to_the_users_item_for_its_utc_day(tm):
    tm.process_log_entry(_log(_user_meta()))
    usage = _usage(tm)
    assert usage["costUsd"] == ONE_CALL and isinstance(usage["costUsd"], Decimal)
    assert (usage["inputTokens"], usage["outputTokens"], usage["calls"]) == (1000, 50, 1)
    assert usage["ttl"] == TTL
    assert datetime.strptime(usage["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")
    assert "cronCostUsd" not in usage and "cronCalls" not in usage
    # One UpdateItem with ADD, so concurrent invocations add up.
    (call,) = tm.identity_table.calls
    assert "ADD costUsd :cost" in call["UpdateExpression"] and call["ConditionExpression"] is None
    # The token-usage row is written as before.
    assert (f"USER#{USER}", f"DATE#{DAY}#CHANNEL#telegram#SESSION#{SESSION}#AGENT#main") in tm.table.items


def test_calls_accumulate(tm):
    for agent in ("main", "subagent", "main"):
        tm.process_log_entry(_log(_user_meta(agent=agent)))
    usage = _usage(tm)
    assert usage["costUsd"] == 3 * ONE_CALL
    assert (usage["inputTokens"], usage["outputTokens"], usage["calls"]) == (3000, 150, 3)


@pytest.mark.parametrize("agent, trigger, scheduled", [
    ("cron", "cron", True),
    ("subagent", "cron", True),     # a sub-agent started by a scheduled turn
    ("main", "chat", False),
    ("subagent", "chat", False),
    ("cron", None, True),           # no trigger: the agent type decides
    ("main", None, False),
])
def test_scheduled_calls_also_add_to_the_cron_fields(tm, agent, trigger, scheduled):
    meta = _user_meta(agent=agent)
    if trigger is None:
        del meta["openclaw.trigger"]
    else:
        meta["openclaw.trigger"] = trigger
    tm.process_log_entry(_log(meta))
    tm.process_log_entry(_log(_user_meta(agent="main")))  # one chat call
    usage = _usage(tm)
    assert usage["calls"] == 2 and usage["costUsd"] == 2 * ONE_CALL
    if scheduled:
        assert (usage["cronCalls"], usage["cronCostUsd"]) == (1, ONE_CALL)
    else:
        assert "cronCalls" not in usage and "cronCostUsd" not in usage


def test_a_malformed_trigger_is_ignored(tm):
    meta = _user_meta(agent="main")
    meta["openclaw.trigger"] = "cron!"
    assert tm.extract_openclaw_metadata(_log(meta))["trigger"] is None


def test_the_day_is_the_calls_utc_day(tm):
    tm.process_log_entry(_log(_user_meta(), timestamp="2026-10-16T23:59:59Z"))
    tm.process_log_entry(_log(_user_meta(), timestamp="2026-10-17T00:00:01Z"))
    assert _usage(tm, day="2026-10-16")["calls"] == 1
    later = _usage(tm, day="2026-10-17")
    assert later["calls"] == 1
    assert later["ttl"] == TTL + 86400


def test_an_older_bridge_record_counts_against_the_actors_user(tm):
    _link(tm, "telegram:1001")
    tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    tm.process_log_entry(_log(_meta(actor="telegram:1001", agent="cron")))
    usage = _usage(tm, user=LEGACY_USER)
    assert usage["calls"] == 2 and usage["cronCalls"] == 1
    # One lookup: the second record is a cache hit.
    assert tm.identity_table.gets == [{"PK": "CHANNEL#telegram:1001", "SK": "PROFILE"}]
    # The token-usage row keeps the actor key, as before.
    assert any(pk == "USER#telegram:1001" for pk, _ in tm.table.items)


def test_the_actor_lookup_is_cached_for_ten_minutes(tm):
    _link(tm, "telegram:1001")
    with patch.object(tm.time, "monotonic", return_value=1000.0):
        tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    with patch.object(tm.time, "monotonic", return_value=1599.0):
        tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    assert len(tm.identity_table.gets) == 1
    with patch.object(tm.time, "monotonic", return_value=1601.0):
        tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    assert len(tm.identity_table.gets) == 2
    assert _usage(tm, user=LEGACY_USER)["calls"] == 3


@pytest.mark.parametrize("profile", [None, {}, {"userId": 42}, {"userId": 'user_1"}'}, {"userId": ""}])
def test_an_unresolved_actor_is_not_counted_and_is_cached(tm, profile):
    if profile is not None:
        tm.identity_table.items[("CHANNEL#slack:U0XXXXXXX", "PROFILE")] = {
            "PK": "CHANNEL#slack:U0XXXXXXX", "SK": "PROFILE", **profile}
    tm.process_log_entry(_log(_meta(actor="slack:U0XXXXXXX", channel="slack")))
    tm.process_log_entry(_log(_meta(actor="slack:U0XXXXXXX", channel="slack")))
    assert not [key for key in tm.identity_table.items if key[0].startswith("USAGE#")]
    assert len(tm.identity_table.gets) == 1
    assert len(tm.table.items) == 1  # the token-usage row is still written


@pytest.mark.parametrize("entry", [
    _log(_meta(actor="default-user", channel="unknown", session="default-session")),
    _log(_user_meta(user=None)),
    _log(None),
], ids=["unbound-actor", "bridge-without-user", "no-request-metadata"])
def test_unbound_calls_are_not_counted_and_not_looked_up(tm, entry):
    tm.process_log_entry(entry)
    assert tm.identity_table.calls == [] and tm.identity_table.gets == []
    assert len(tm.table.items) == 1


def test_a_failed_item_write_still_writes_the_row_and_the_metrics(tm, caplog):
    tm.identity_table.update_error = _throttle("UpdateItem")
    with caplog.at_level(logging.WARNING):
        result = tm.handler(_cwl_event(_log(_user_meta())), None)
    assert result == {"processed": 1, "errors": 0, "total": 1}
    assert len(tm.table.items) == 1
    assert tm.cloudwatch.put_metric_data.called
    assert "Usage aggregate not written: ClientError" in caplog.text
    assert _usage(tm) is None


def test_a_failed_actor_lookup_is_retried_on_the_next_record(tm, caplog):
    _link(tm, "telegram:1001")
    tm.identity_table.get_error = _throttle("GetItem")
    with caplog.at_level(logging.WARNING):
        tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    assert "Usage aggregate not written: ClientError" in caplog.text
    assert len(tm.table.items) == 1 and tm.cloudwatch.put_metric_data.called
    tm.identity_table.get_error = None
    tm.process_log_entry(_log(_meta(actor="telegram:1001")))
    assert _usage(tm, user=LEGACY_USER)["calls"] == 1
    assert len(tm.identity_table.gets) == 2


def test_without_an_identity_table_no_item_is_written(tm):
    tm.identity_table = None
    tm.process_log_entry(_log(_user_meta()))
    assert len(tm.table.items) == 1


def test_no_actor_or_platform_id_reaches_the_logs(tm, caplog):
    actor = "whatsapp:15551234567"
    _link(tm, actor)
    tm.identity_table.update_error = _throttle("UpdateItem")
    with caplog.at_level(logging.DEBUG):
        tm.handler(_cwl_event(_log(_meta(actor=actor, channel="whatsapp"))), None)
    assert "15551234567" not in caplog.text
    digest = hashlib.sha256(actor.encode()).hexdigest()[:12]
    assert f"actor=whatsapp:sha256:{digest}" in caplog.text


def test_keys_match_the_readers():
    mod = _load_module()
    assert (mod.USAGE_PK_PREFIX, mod.USAGE_DAY_PREFIX) == (usage_limits.PK_PREFIX, usage_limits.DAY_PREFIX)


def test_the_readers_see_what_the_lambda_wrote(tm):
    """usage_limits reads the item the Lambda wrote, with the Lambda's own cost estimate."""
    for _ in range(3):
        tm.process_log_entry(_log(_user_meta(), inp=1_000_000, out=10_000))  # $4.20 each
    tm.process_log_entry(_log(_user_meta(agent="cron"), inp=500_000, out=0))  # $2.00

    class Reader:
        def query(self, **kwargs):
            values = kwargs["ExpressionAttributeValues"]
            return {"Items": [item for (pk, sk), item in tm.identity_table.items.items()
                              if pk == values[":pk"] and values[":cap"] <= sk <= values[":day"]]}

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    usage = usage_limits.over_limit(Reader(), USER, Decimal(10), now)
    assert usage is not None and usage.cost == Decimal("14.60")
    assert (usage.calls, usage.cron_calls, usage.input_tokens, usage.output_tokens) == (4, 1, 3_500_000, 30_000)
    assert usage_limits.over_limit(Reader(), USER, Decimal(20), now) is None
