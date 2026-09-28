"""Unit tests for lambda/token_metrics/index.py model pricing.

Hermetic: boto3.resource / boto3.client are patched while the module is
imported, so no AWS client or credential lookup happens.

    pytest tests/test_token_metrics_pricing.py -v
"""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[1]
_LAMBDA = _ROOT / "lambda" / "token_metrics" / "index.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("token_metrics_index", _LAMBDA)
    mod = importlib.util.module_from_spec(spec)
    env = {"TABLE_NAME": "test-token-usage", "AWS_DEFAULT_REGION": "us-east-1"}
    with patch.dict(os.environ, env), patch("boto3.resource", MagicMock()), patch(
        "boto3.client", MagicMock()
    ):
        spec.loader.exec_module(mod)
    return mod


tm = _load_module()

MILLION = 1_000_000

# Claude Opus 5.5 (Amazon Bedrock Edition), USD per 1M tokens, from the AWS
# Price List API (AmazonBedrockFoundationModels, ap-southeast-2 and us-east-1).
OPUS_55_GLOBAL = {"input": 4.00, "output": 20.00}
OPUS_55_STANDARD = {"input": 4.40, "output": 22.00}


class TestOpus55Pricing(unittest.TestCase):
    def _assert_priced(self, model_id, expected):
        self.assertAlmostEqual(
            tm.estimate_cost(model_id, MILLION, 0), expected["input"], places=6, msg=model_id
        )
        self.assertAlmostEqual(
            tm.estimate_cost(model_id, 0, MILLION), expected["output"], places=6, msg=model_id
        )

    def test_opus_5_5_global_profile_uses_global_price(self):
        self._assert_priced("global.anthropic.claude-opus-5-5", OPUS_55_GLOBAL)

    def test_opus_5_5_geo_and_base_ids_use_standard_price(self):
        for model_id in (
            "au.anthropic.claude-opus-5-5",
            "us.anthropic.claude-opus-5-5",
            "eu.anthropic.claude-opus-5-5",
            "apac.anthropic.claude-opus-5-5",
            "anthropic.claude-opus-5-5",
        ):
            with self.subTest(model_id=model_id):
                self._assert_priced(model_id, OPUS_55_STANDARD)

    def test_opus_5_5_inference_profile_arn_uses_global_price(self):
        arn = (
            "arn:aws:bedrock:ap-southeast-2:123456789012:"
            "inference-profile/global.anthropic.claude-opus-5-5"
        )
        self._assert_priced(arn, OPUS_55_GLOBAL)

    def test_opus_5_5_mixed_tokens(self):
        cost = tm.estimate_cost("global.anthropic.claude-opus-5-5", 12_000, 3_000)
        self.assertAlmostEqual(cost, 12_000 / MILLION * 4.00 + 3_000 / MILLION * 20.00, places=8)


class TestExistingPricingUnchanged(unittest.TestCase):
    def test_unknown_model_falls_back_to_default(self):
        cost = tm.estimate_cost("vendor.some-new-model-v9", MILLION, MILLION)
        self.assertAlmostEqual(
            cost, tm.DEFAULT_PRICING["input"] + tm.DEFAULT_PRICING["output"], places=6
        )

    def test_nova_global_prefix_still_matches(self):
        self.assertAlmostEqual(
            tm.estimate_cost("global.amazon.nova-2-lite-v1:0", MILLION, 0), 0.30, places=6
        )


if __name__ == "__main__":
    unittest.main()
