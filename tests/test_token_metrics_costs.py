"""Token metrics Lambda: Opus 4.6 price, cache token pricing, unknown-model warning.

Prices are USD per 1M tokens from the AWS Price List bulk offer file for
AmazonBedrockFoundationModels (publication 2026-10-12T08:40:08Z), the same
values in us-east-1 and ap-southeast-2. Hermetic: boto3 is patched at import.

    pytest tests/test_token_metrics_costs.py -v
"""

import importlib.util
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA = Path(__file__).resolve().parents[1] / "lambda" / "token_metrics" / "index.py"
MILLION = 1_000_000


def _load_module():
    spec = importlib.util.spec_from_file_location("token_metrics_costs_under_test", _LAMBDA)
    mod = importlib.util.module_from_spec(spec)
    env = {"TABLE_NAME": "test-token-usage", "AWS_DEFAULT_REGION": "us-east-1"}
    with patch.dict(os.environ, env), patch("boto3.resource", MagicMock()), \
            patch("boto3.client", MagicMock()):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tm():
    return _load_module()


# model id -> (input, output, cache read, cache write 5m)
LISTED = {
    "global.anthropic.claude-opus-4-6-v1": (5.00, 25.00, 0.50, 6.25),
    "au.anthropic.claude-opus-4-6-v1": (5.50, 27.50, 0.55, 6.875),
    "us.anthropic.claude-opus-4-6-v1": (5.50, 27.50, 0.55, 6.875),
    "anthropic.claude-opus-4-6-v1": (5.50, 27.50, 0.55, 6.875),
    "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/global.anthropic.claude-opus-4-6-v1":
        (5.00, 25.00, 0.50, 6.25),
    "global.anthropic.claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "au.anthropic.claude-opus-5-5": (4.40, 22.00, 0.22, 5.50),
    "anthropic.claude-opus-5-5": (4.40, 22.00, 0.22, 5.50),
}


@pytest.mark.parametrize("model_id", list(LISTED))
def test_listed_input_output_and_cache_rates(tm, model_id):
    inp, out, read, write = LISTED[model_id]
    assert tm.estimate_cost(model_id, MILLION, 0) == pytest.approx(inp)
    assert tm.estimate_cost(model_id, 0, MILLION) == pytest.approx(out)
    assert tm.estimate_cost(model_id, 0, 0, cache_read_tokens=MILLION) == pytest.approx(read)
    assert tm.estimate_cost(model_id, 0, 0, cache_write_tokens=MILLION) == pytest.approx(write)


def test_mixed_cost_sums_all_four_token_kinds(tm):
    cost = tm.estimate_cost("global.anthropic.claude-opus-4-6-v1", 1_000, 2_000,
                            cache_read_tokens=30_000, cache_write_tokens=4_000)
    assert cost == pytest.approx((1_000 * 5 + 2_000 * 25 + 30_000 * 0.5 + 4_000 * 6.25) / MILLION)


def test_model_without_cache_rates_bills_cache_tokens_at_input_rate(tm):
    cost = tm.estimate_cost("amazon.nova-pro-v1:0", 0, 0,
                            cache_read_tokens=MILLION, cache_write_tokens=MILLION)
    assert cost == pytest.approx(0.80 * 2)


def test_no_key_is_shadowed_by_an_earlier_less_specific_key(tm):
    keys = list(tm.MODEL_PRICING)
    shadowed = [(a, b) for i, a in enumerate(keys) for b in keys[i + 1:] if a in b]
    assert not shadowed, shadowed


def test_unknown_model_uses_default_and_warns_once_per_model(tm, caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert tm.estimate_cost("vendor.new-model-v9", MILLION, 0) == pytest.approx(
                tm.DEFAULT_PRICING["input"])
        tm.estimate_cost("vendor.other-model-v1", MILLION, 0)
        tm.estimate_cost("global.anthropic.claude-opus-5-5", MILLION, 0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.args for r in warnings] == [("vendor.new-model-v9",), ("vendor.other-model-v1",)]
    for record in warnings:
        assert "%s" in record.msg and "vendor." not in record.msg  # constant format string


def _record_cost(tm, log_entry):
    captured = []
    with patch.object(tm, "write_to_dynamodb", captured.append), \
            patch.object(tm, "publish_metrics", lambda record: None):
        tm.process_log_entry(log_entry)
    return captured


def test_documented_invocation_log_schema_is_counted_with_cache_tokens(tm):
    # Shape from the Bedrock "Log entry format" docs: counts nested under input/output.
    entry = {
        "schemaType": "ModelInvocationLog", "timestamp": "2026-10-15T00:00:00Z",
        "modelId": "global.anthropic.claude-opus-5-5",
        "input": {"inputTokenCount": 1_000, "cacheReadInputTokenCount": 50_000,
                  "cacheWriteInputTokenCount": 2_000},
        "output": {"outputTokenCount": 500},
    }
    [record] = _record_cost(tm, entry)
    assert (record["input_tokens"], record["output_tokens"]) == (1_000, 500)
    assert (record["cache_read_tokens"], record["cache_write_tokens"]) == (50_000, 2_000)
    assert record["estimated_cost_usd"] == pytest.approx(
        (1_000 * 4 + 500 * 20 + 50_000 * 0.2 + 2_000 * 5) / MILLION)


def test_converse_usage_cache_fields_are_counted(tm):
    # Converse TokenUsage names: cacheReadInputTokens / cacheWriteInputTokens.
    entry = {
        "timestamp": "2026-10-15T00:00:00Z", "modelId": "au.anthropic.claude-opus-5-5",
        "input": {"inputTokenCount": 100},
        "output": {"outputTokenCount": 10, "outputBodyJson": {"usage": {
            "inputTokens": 100, "outputTokens": 10,
            "cacheReadInputTokens": 9_000, "cacheWriteInputTokens": 300}}},
    }
    [record] = _record_cost(tm, entry)
    assert (record["cache_read_tokens"], record["cache_write_tokens"]) == (9_000, 300)
    assert record["estimated_cost_usd"] == pytest.approx(
        (100 * 4.4 + 10 * 22 + 9_000 * 0.22 + 300 * 5.5) / MILLION)


def test_flat_legacy_shape_still_counted(tm):
    entry = {"timestamp": "2026-10-15T00:00:00Z", "modelId": "amazon.nova-pro-v1:0",
             "inputTokenCount": 1_000, "outputTokenCount": 1_000}
    [record] = _record_cost(tm, entry)
    assert record["estimated_cost_usd"] == pytest.approx((1_000 * 0.8 + 1_000 * 3.2) / MILLION)
    assert (record["cache_read_tokens"], record["cache_write_tokens"]) == (0, 0)
