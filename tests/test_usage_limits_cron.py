"""Per-user daily cost limit (per_user_daily_cost_usd) in the Cron Lambda.

A schedule whose user is at or over today's limit is checked once per fire,
after verify_cron_event and before any AgentCore call: the synchronous path
skips the warm-up and the turn, and the async path (cron_async) starts no
Step Functions execution. A job that has already started is not checked
again. The user gets one skip notice per UTC day, and the skip log line
matches none of the scheduled-task failure alarm terms.
"""

import copy
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.test_runtime_routing import USER, FakeRuntime, FakeTable, cron_event, cron_table, load_lambda
from tests.test_runtime_routing_synth import ACCOUNT, REGION
import usage_limits  # noqa: E402  (lambda/shared/python, on sys.path via tests.test_runtime_routing)

SM_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:openclaw-cron-runner"
READY = {"status": "ready", "phase": "USER_READY"}
DAY = "2026-11-05"
NOW = datetime(2026, 11, 5, 17, 48, tzinfo=timezone.utc)
SKIPPED = ("[Scheduled: Desk] Skipped: today's usage limit is reached. "
           "It runs again at its next scheduled time after 00:00 UTC.")
# stacks/health_alarms.py: openclaw-scheduled-task-failures and openclaw-runtime-session-unstartable.
ALARM_TERMS = ("did not complete", "Warmup timed out", "Status: timeout", "could not start runtime session")


class Context:
    def get_remaining_time_in_millis(self):
        return 900_000


class UsageTable(FakeTable):
    """cron_table() plus the USAGE# query and the conditional skip-notice write."""

    def __init__(self):
        super().__init__()
        self.items.update(cron_table().items)
        self.queries = []
        self.notice_writes = []
        self.query_error = None
        self.notice_error = None
        self.race = False  # another fire records the notice between our read and our write

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.query_error:
            raise self.query_error
        values = kwargs["ExpressionAttributeValues"]
        items = [copy.deepcopy(item) for (pk, sk), item in sorted(self.items.items())
                 if pk == values[":pk"] and values[":cap"] <= sk <= values[":day"]]
        if self.race:
            self.items[(f"USAGE#{USER}", f"DAY#{DAY}")]["cronNoticeAt"] = "2026-11-05T17:47:59Z"
        return {"Items": items}

    def update_item(self, **kwargs):
        if not kwargs["Key"]["PK"].startswith("USAGE#"):
            return super().update_item(**kwargs)
        self.notice_writes.append(kwargs)
        if self.notice_error:
            raise self.notice_error
        assert kwargs["UpdateExpression"] == "SET #notice = :at"
        assert kwargs["ConditionExpression"] == "attribute_exists(PK) AND attribute_not_exists(#notice)"
        assert kwargs["ExpressionAttributeNames"] == {"#notice": "cronNoticeAt"}
        item = self.items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        if item is None or "cronNoticeAt" in item:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")
        item["cronNoticeAt"] = kwargs["ExpressionAttributeValues"][":at"]
        return {}


def day_item(cost, **extra):
    return {"PK": f"USAGE#{USER}", "SK": f"DAY#{DAY}", "costUsd": Decimal(cost), "calls": Decimal(9), **extra}


@pytest.fixture(autouse=True)
def clock():
    with patch.object(usage_limits, "utc_now", return_value=NOW):
        yield


def _lambda(responses=(), async_on=False, limit=5, cost=None):
    module = load_lambda("cron")
    module.identity_table = UsageTable()
    if cost is not None:
        module.identity_table.items[(f"USAGE#{USER}", f"DAY#{DAY}")] = day_item(cost)
    module.agentcore_client = FakeRuntime(list(responses))
    module.CRON_ASYNC_STATE_MACHINE_ARN = SM_ARN if async_on else ""
    module.PER_USER_DAILY_COST_USD = Decimal(limit)
    module.sfn_client = MagicMock()
    module.deliver_response = MagicMock()
    module.deliver_brief_audio = MagicMock()
    module.find_run_audio_key = MagicMock(return_value=None)
    module.warmup_and_wait.__globals__["WARMUP_POLL_INTERVAL_SECONDS"] = 0
    return module


def _event():
    return {**cron_event(), "scheduleName": "Desk"}


def _sent(module):
    return [call.args[2] for call in module.deliver_response.call_args_list]


def _actions(module):
    return [json.loads(call["payload"])["action"] for call in module.agentcore_client.calls]


def _skipped(response):
    return response == {"statusCode": 200, "body": "Skipped: daily usage limit reached"}


# --- synchronous path (cron_async=false) ------------------------------------------


def test_sync_fire_over_the_limit_runs_no_warmup_and_no_turn():
    module = _lambda(cost="5.03")
    assert _skipped(module.handler(_event(), Context()))
    assert module.agentcore_client.calls == []
    assert _sent(module) == [SKIPPED]
    module.deliver_response.assert_called_once_with("telegram", "123", SKIPPED)
    (query,) = module.identity_table.queries
    assert query["ExpressionAttributeValues"] == {":pk": f"USAGE#{USER}", ":cap": "CAP", ":day": f"DAY#{DAY}"}
    assert module.identity_table.items[(f"USAGE#{USER}", f"DAY#{DAY}")]["cronNoticeAt"] == "2026-11-05T17:48:00Z"


def test_a_second_fire_on_the_same_day_sends_nothing():
    module = _lambda(cost="7")
    assert _skipped(module.handler(_event(), Context()))
    assert _skipped(module.handler(_event(), Context()))
    assert _sent(module) == [SKIPPED]
    assert len(module.identity_table.notice_writes) == 1  # the second read already sees cronNoticeAt
    assert module.agentcore_client.calls == []


def test_concurrent_fires_send_one_notice():
    module = _lambda(cost="7")
    module.identity_table.race = True
    assert _skipped(module.handler(_event(), Context()))
    assert len(module.identity_table.notice_writes) == 1
    assert _sent(module) == []


def test_a_failed_notice_write_sends_no_notice_and_still_skips(caplog):
    module = _lambda(cost="7")
    module.identity_table.notice_error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "UpdateItem")
    with caplog.at_level(logging.WARNING):
        assert _skipped(module.handler(_event(), Context()))
    assert _sent(module) == []
    assert f"Usage notice record write failed for user {USER}: ClientError" in caplog.text
    assert module.agentcore_client.calls == []


def test_sync_fire_under_the_limit_runs_as_before():
    module = _lambda([READY, {"response": "done"}], cost="4.99")
    assert module.handler(_event(), Context())["statusCode"] == 200
    assert _actions(module) == ["warmup", "cron"]
    assert _sent(module) == ["done"]
    assert module.identity_table.notice_writes == []


def test_the_limit_off_reads_nothing():
    module = _lambda([READY, {"response": "done"}], limit=0, cost="500")
    assert module.handler(_event(), Context())["statusCode"] == 200
    assert module.identity_table.queries == []
    assert _sent(module) == ["done"]


def test_an_exempt_user_runs():
    module = _lambda([READY, {"response": "done"}], cost="500")
    module.identity_table.items[(f"USAGE#{USER}", "CAP")] = {
        "PK": f"USAGE#{USER}", "SK": "CAP", "dailyCostUsd": Decimal(0)}
    assert module.handler(_event(), Context())["statusCode"] == 200
    assert _sent(module) == ["done"]


def test_a_failed_read_runs_the_schedule(caplog):
    module = _lambda([READY, {"response": "done"}], cost="500")
    module.identity_table.query_error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "Query")
    with caplog.at_level(logging.WARNING):
        assert module.handler(_event(), Context())["statusCode"] == 200
    assert f"Usage read failed for user {USER}: ClientError" in caplog.text
    assert _sent(module) == ["done"]


def test_an_unverified_schedule_is_refused_before_the_usage_read():
    module = _lambda(cost="7")
    module.identity_table.items.pop((f"USER#{USER}", "CRON#schedule123"))
    assert module.handler(_event(), Context())["statusCode"] == 403
    assert module.identity_table.queries == []
    assert _sent(module) == []


def test_the_skip_log_line_avoids_the_failure_alarm_terms(caplog):
    module = _lambda(cost="7")
    with caplog.at_level(logging.DEBUG):
        module.handler(_event(), Context())
    assert "Scheduled task schedule123 skipped: daily usage limit reached" in caplog.text
    for term in ALARM_TERMS:
        assert term not in caplog.text, term
    for term in ALARM_TERMS:  # the notice is not logged either
        assert term not in SKIPPED


# --- async path (cron_async, the default) -----------------------------------------


def test_async_fire_over_the_limit_starts_no_execution():
    module = _lambda(async_on=True, cost="5.03")
    assert _skipped(module.handler(_event(), Context()))
    module.sfn_client.start_execution.assert_not_called()
    assert module.agentcore_client.calls == []
    assert _sent(module) == [SKIPPED]


def test_async_fire_under_the_limit_starts_the_execution():
    module = _lambda(async_on=True, cost="1")
    assert module.handler(_event(), Context())["statusCode"] == 202
    module.sfn_client.start_execution.assert_called_once()
    assert _sent(module) == []


def test_a_started_job_polls_and_delivers_over_the_limit():
    """A run that has started finishes: the poll and deliver phases read no usage."""
    module = _lambda([{"status": "running"}], async_on=True, cost="50")
    state = {**_event(), "jobId": "job-1", "cronPhase": "poll", "jobDeadline": (time.time() + 600) * 1000}
    assert module.handler(state, Context())["next"] == "wait"
    assert _actions(module) == ["cron_status"]
    done = {**_event(), "jobId": "job-1", "cronPhase": "deliver", "outcome": "completed", "response": "the brief",
            "runStarted": time.time() - 60, "runFinished": time.time()}
    assert module.handler(done, Context())["next"] == "done"
    assert _sent(module) == ["the brief"]
    assert module.identity_table.queries == []


def test_a_start_phase_is_not_checked_again():
    module = _lambda([READY, {"status": "accepted", "jobId": "job-1", "deadline": 1}], async_on=True, cost="50")
    out = module.handler({**_event(), "jobId": "job-1", "cronPhase": "start"}, Context())
    assert out["next"] == "wait"
    assert module.identity_table.queries == []
