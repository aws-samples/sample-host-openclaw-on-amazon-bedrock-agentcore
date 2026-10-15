"""Token metrics Lambda: DynamoDB rows carry a cumulative cost and GSI3 ranks by it.

Each row (PK USER#<actor>, SK DATE#<day>#CHANNEL#<ch>#SESSION#<sess>) is the
running total for that user/day/channel/session. The tokens were accumulated
with ADD but the cost was never stored, and GSI3SK held only the latest call's
cost, so the GSI3 "top spenders per day" ordering was wrong.

Hermetic: boto3 is patched at import and the table is an in-memory stand-in
that applies the SET / ADD / if_not_exists / equality-condition subset of the
UpdateItem API the Lambda uses, with boto3's Decimal-only number rule.

    pytest tests/test_token_metrics_totals.py -v
"""

import importlib.util
import logging
import os
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_LAMBDA = Path(__file__).resolve().parents[1] / "lambda" / "token_metrics" / "index.py"
MILLION = 1_000_000
CENT8 = Decimal("0.00000001")


class FakeTable:
    """In-memory subset of a boto3 DynamoDB Table resource's update_item."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.before_update = []  # callables run before each update is applied

    @staticmethod
    def _check_number(value):
        if isinstance(value, float):
            # boto3's TypeSerializer refuses floats for DynamoDB Numbers.
            raise TypeError("Float types are not supported. Use Decimal types instead.")

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None,
                    ReturnValues="NONE"):
        self.calls.append({"UpdateExpression": UpdateExpression,
                           "ConditionExpression": ConditionExpression,
                           "ReturnValues": ReturnValues})
        for hook in list(self.before_update):
            hook(self, Key)
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues
        for value in values.values():
            self._check_number(value)

        def name(token):
            token = token.strip()
            return names.get(token, token)

        key = (Key["PK"], Key["SK"])
        current = dict(self.items.get(key, {}))

        if ConditionExpression:
            left, right = (part.strip() for part in ConditionExpression.split("="))
            if current.get(name(left)) != values[right]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                             "Message": "The conditional request failed"}},
                                  "UpdateItem")

        new = dict(current)
        new.update(Key)
        touched = []
        for clause, body in re.findall(r"(SET|ADD)\s+(.*?)(?=\s+(?:SET|ADD)\s|$)",
                                       UpdateExpression.strip(), flags=re.S):
            parts = [p for p in re.split(r",(?![^(]*\))", body) if p.strip()]
            for part in parts:
                if clause == "SET":
                    attr, expr = (s.strip() for s in part.split("=", 1))
                    attr = name(attr)
                    fn = re.fullmatch(r"if_not_exists\(\s*([^,]+),\s*(:\w+)\s*\)", expr)
                    if fn:
                        existing = name(fn.group(1))
                        new[attr] = current[existing] if existing in current else values[fn.group(2)]
                    else:
                        new[attr] = values[expr]
                else:
                    attr, placeholder = part.split()
                    attr = name(attr)
                    new[attr] = current.get(attr, 0) + values[placeholder]
                touched.append(attr)
        self.items[key] = new
        if ReturnValues == "UPDATED_NEW":
            return {"Attributes": {a: new[a] for a in touched}}
        return {}

    def gsi3(self, date_str):
        """Rows for one day, highest GSI3SK first (ScanIndexForward=False)."""
        rows = [i for i in self.items.values()
                if i.get("GSI3PK") == f"DATE#{date_str}" and "GSI3SK" in i]
        return sorted(rows, key=lambda i: i["GSI3SK"], reverse=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("token_metrics_totals_under_test", _LAMBDA)
    mod = importlib.util.module_from_spec(spec)
    env = {"TABLE_NAME": "test-token-usage", "AWS_DEFAULT_REGION": "us-east-1"}
    with patch.dict(os.environ, env), patch("boto3.resource", MagicMock()), \
            patch("boto3.client", MagicMock()):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tm():
    mod = _load_module()
    mod.table = FakeTable()
    mod.cloudwatch = MagicMock()
    return mod


def _record(cost, actor="user-a", session="sess-1", channel="telegram",
            model="global.anthropic.claude-opus-5-5", day="2026-10-15",
            inp=100, out=10):
    return {
        "actor_id": actor, "session_id": session, "channel": channel,
        "model_id": model, "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "total_tokens": inp + out, "estimated_cost_usd": cost,
        "timestamp": f"{day}T01:00:00+00:00", "date": day,
    }


def _row(tm, actor="user-a", session="sess-1", channel="telegram", day="2026-10-15"):
    return tm.table.items[(f"USER#{actor}", f"DATE#{day}#CHANNEL#{channel}#SESSION#{session}")]


def _sort_key(total):
    return f"COST#{Decimal(total).quantize(CENT8):015.8f}"


def test_two_calls_accumulate_tokens_and_cost(tm):
    tm.write_to_dynamodb(_record(0.146076, inp=1_000, out=100))
    tm.write_to_dynamodb(_record(0.0213, inp=200, out=50))
    row = _row(tm)
    assert (row["inputTokens"], row["outputTokens"], row["totalTokens"]) == (1_200, 150, 1_350)
    assert row["invocationCount"] == 2
    assert isinstance(row["estimatedCostUSD"], Decimal)
    assert row["estimatedCostUSD"] == Decimal("0.167376")


def test_gsi3sk_is_the_cumulative_total_after_each_call(tm):
    running = Decimal(0)
    for cost in (0.146076, 0.00000004, 1.25, 0.333333333):
        tm.write_to_dynamodb(_record(cost))
        running += Decimal(str(cost)).quantize(CENT8)
        row = _row(tm)
        assert row["estimatedCostUSD"] == running
        assert row["GSI3SK"] == _sort_key(running)
        assert len(row["GSI3SK"]) == len("COST#") + 15


def test_rows_with_many_small_calls_outrank_one_bigger_call(tm):
    # Before the fix GSI3 ranked by the latest call only, so B (one $0.20 call)
    # sorted above A (three $0.10 calls = $0.30).
    for _ in range(3):
        tm.write_to_dynamodb(_record(0.10, actor="user-a"))
    tm.write_to_dynamodb(_record(0.20, actor="user-b"))
    tm.write_to_dynamodb(_record(9.5, actor="user-c"))
    tm.write_to_dynamodb(_record(0.75, actor="user-d"))
    tm.write_to_dynamodb(_record(9.5, actor="user-d"))  # d = 10.25, crosses a digit
    ranked = [row["actorId"] for row in tm.table.gsi3("2026-10-15")]
    assert ranked == ["user-d", "user-c", "user-a", "user-b"]


def test_sort_key_string_order_matches_numeric_order(tm):
    totals = [Decimal(v) for v in ("0", "0.00000001", "0.3", "9.99999999", "10",
                                   "123.45", "99999.5", "999999.99999999")]
    keys = [tm.cost_sort_key(t) for t in totals]
    assert keys == sorted(keys)
    assert len(set(map(len, keys))) == 1


def test_sort_key_is_capped_so_huge_totals_still_sort_last(tm, caplog):
    with caplog.at_level(logging.WARNING):
        huge = tm.cost_sort_key(Decimal("1234567.5"))
    assert huge == "COST#999999.99999999"
    assert huge >= tm.cost_sort_key(Decimal("999999.99999999"))
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "%s" in warning.msg and "1234567" not in warning.msg


def test_concurrent_writer_between_add_and_sort_key_update(tm, caplog):
    """Another Lambda adds cost after our ADD; our conditional SET must not regress it."""
    tm.write_to_dynamodb(_record(0.50))

    def concurrent_writer(table, key):
        if len(table.calls) == 4:  # our second call's sort-key update is about to run
            table.before_update.clear()
            tm.write_to_dynamodb(_record(2.00))

    tm.table.before_update.append(concurrent_writer)
    with caplog.at_level(logging.WARNING):
        tm.write_to_dynamodb(_record(0.25))  # must not raise

    row = _row(tm)
    assert row["estimatedCostUSD"] == Decimal("2.75")
    assert row["GSI3SK"] == _sort_key("2.75")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "%s" in warnings[0].msg and "user-a" not in warnings[0].msg  # constant format


def test_sort_key_update_is_conditional_on_the_total_read(tm):
    tm.write_to_dynamodb(_record(0.5))
    add_call, sort_call = tm.table.calls
    assert "estimatedCostUSD" in add_call["UpdateExpression"]
    assert add_call["ReturnValues"] == "UPDATED_NEW"
    assert "GSI3SK" in sort_call["UpdateExpression"]
    assert sort_call["ConditionExpression"]


def test_other_sort_key_update_errors_still_raise(tm):
    def throttle(table, key):
        if len(table.calls) == 2:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                         "Message": "slow down"}}, "UpdateItem")

    tm.table.before_update.append(throttle)
    with pytest.raises(ClientError):
        tm.write_to_dynamodb(_record(0.5))


def test_row_written_before_the_fix_gets_a_cumulative_sort_key(tm):
    # Pre-fix rows have tokens but no estimatedCostUSD and a last-call GSI3SK.
    key = ("USER#user-a", "DATE#2026-10-15#CHANNEL#telegram#SESSION#sess-1")
    tm.table.items[key] = {"PK": key[0], "SK": key[1], "inputTokens": 500,
                           "GSI3PK": "DATE#2026-10-15", "GSI3SK": "COST#000000.14607600"}
    tm.write_to_dynamodb(_record(0.01))
    row = _row(tm)
    assert row["estimatedCostUSD"] == Decimal("0.01")
    assert row["GSI3SK"] == _sort_key("0.01")


def test_unknown_model_default_pricing_path_writes_cost_and_metrics(tm):
    entry = {"timestamp": "2026-10-15T00:00:00Z", "modelId": "vendor.new-model-v9",
             "input": {"inputTokenCount": 2_000}, "output": {"outputTokenCount": 1_000}}
    tm.process_log_entry(entry)
    tm.process_log_entry(entry)
    expected_one = Decimal(str((2_000 * tm.DEFAULT_PRICING["input"]
                                + 1_000 * tm.DEFAULT_PRICING["output"]) / MILLION))
    [row] = tm.table.items.values()
    assert row["modelId"] == "vendor.new-model-v9"
    assert row["estimatedCostUSD"] == 2 * expected_one
    assert row["GSI3SK"] == _sort_key(2 * expected_one)
    # CloudWatch path unchanged: per-call float cost, not the cumulative total.
    sent = [d for c in tm.cloudwatch.put_metric_data.call_args_list
            for d in c.kwargs["MetricData"] if d["MetricName"] == "EstimatedCostUSD"]
    assert [d["Value"] for d in sent] == [float(expected_one)] * 4
