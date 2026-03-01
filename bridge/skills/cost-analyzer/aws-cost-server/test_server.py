"""Tests for aws-cost-server/server.py — Cost Explorer + CloudWatch Logs MCP server.

Tests cover:
  1. Cost Explorer response parsing and aggregation
  2. CloudWatch Logs response parsing and aggregation
  3. Error handling for API failures

Run: python -m pytest test_server.py -v
      (requires `mcp` and `boto3` packages)

Skips gracefully if `mcp` package is not installed.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set required env vars before importing
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("BEDROCK_LOG_GROUP_NAME", "/aws/bedrock/invocation-logs")

# Check if mcp is available (installed in Docker, may not be local)
try:
    from mcp.server.fastmcp import FastMCP

    HAS_MCP = True
except ImportError:
    HAS_MCP = False


@unittest.skipUnless(HAS_MCP, "mcp package not installed (install via: pip install mcp)")
class TestGetDetailedBreakdownByDay(unittest.TestCase):
    """Test get_detailed_breakdown_by_day tool."""

    def setUp(self):
        # Clear module cache to pick up fresh mocks
        if "server" in sys.modules:
            del sys.modules["server"]

    @patch("boto3.client")
    def test_parses_cost_explorer_response(self, mock_boto_client):
        mock_ce = MagicMock()
        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-02-27", "End": "2026-02-28"},
                    "Groups": [
                        {
                            "Keys": ["Amazon Bedrock"],
                            "Metrics": {
                                "BlendedCost": {"Amount": "1.234567", "Unit": "USD"},
                                "UsageQuantity": {"Amount": "100", "Unit": "N/A"},
                            },
                        },
                        {
                            "Keys": ["Amazon S3"],
                            "Metrics": {
                                "BlendedCost": {"Amount": "0.050000", "Unit": "USD"},
                                "UsageQuantity": {"Amount": "500", "Unit": "N/A"},
                            },
                        },
                        {
                            "Keys": ["Tax"],
                            "Metrics": {
                                "BlendedCost": {"Amount": "0.000000", "Unit": "USD"},
                                "UsageQuantity": {"Amount": "0", "Unit": "N/A"},
                            },
                        },
                    ],
                }
            ]
        }
        mock_boto_client.return_value = mock_ce

        import server

        result_str = server.get_detailed_breakdown_by_day(7)
        result = json.loads(result_str)

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["date"], "2026-02-27")
        # Tax service has 0 cost and 0 usage — filtered out
        self.assertEqual(len(result[0]["services"]), 2)
        # Services sorted by cost descending
        self.assertEqual(result[0]["services"][0]["service"], "Amazon Bedrock")
        self.assertAlmostEqual(result[0]["services"][0]["cost_usd"], 1.234567, places=5)
        self.assertEqual(result[0]["services"][1]["service"], "Amazon S3")
        # Total is sum of non-zero services
        self.assertAlmostEqual(result[0]["total"], 1.284567, places=5)

    @patch("boto3.client")
    def test_handles_cost_explorer_error(self, mock_boto_client):
        mock_ce = MagicMock()
        mock_ce.get_cost_and_usage.side_effect = Exception(
            "AccessDeniedException: Not authorized"
        )
        mock_boto_client.return_value = mock_ce

        import server

        result_str = server.get_detailed_breakdown_by_day(7)
        result = json.loads(result_str)

        self.assertIn("error", result)
        self.assertIn("AccessDeniedException", result["error"])

    @patch("boto3.client")
    def test_handles_empty_response(self, mock_boto_client):
        mock_ce = MagicMock()
        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
        mock_boto_client.return_value = mock_ce

        import server

        result_str = server.get_detailed_breakdown_by_day(7)
        result = json.loads(result_str)

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    @patch("boto3.client")
    def test_caps_days_at_90(self, mock_boto_client):
        mock_ce = MagicMock()
        mock_ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
        mock_boto_client.return_value = mock_ce

        import server

        server.get_detailed_breakdown_by_day(365)

        call_args = mock_ce.get_cost_and_usage.call_args
        time_period = call_args[1]["TimePeriod"] if "TimePeriod" in (call_args[1] or {}) else call_args.kwargs["TimePeriod"]
        # Start date should be ~90 days ago, not 365
        from datetime import datetime, timedelta, timezone

        expected_start = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        self.assertEqual(time_period["Start"], expected_start)

    @patch("boto3.client")
    def test_multiple_days_response(self, mock_boto_client):
        mock_ce = MagicMock()
        mock_ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-02-26", "End": "2026-02-27"},
                    "Groups": [
                        {
                            "Keys": ["Amazon Bedrock"],
                            "Metrics": {
                                "BlendedCost": {"Amount": "2.00", "Unit": "USD"},
                                "UsageQuantity": {"Amount": "200", "Unit": "N/A"},
                            },
                        },
                    ],
                },
                {
                    "TimePeriod": {"Start": "2026-02-27", "End": "2026-02-28"},
                    "Groups": [
                        {
                            "Keys": ["Amazon Bedrock"],
                            "Metrics": {
                                "BlendedCost": {"Amount": "3.00", "Unit": "USD"},
                                "UsageQuantity": {"Amount": "300", "Unit": "N/A"},
                            },
                        },
                    ],
                },
            ]
        }
        mock_boto_client.return_value = mock_ce

        import server

        result_str = server.get_detailed_breakdown_by_day(2)
        result = json.loads(result_str)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["date"], "2026-02-26")
        self.assertEqual(result[1]["date"], "2026-02-27")


@unittest.skipUnless(HAS_MCP, "mcp package not installed (install via: pip install mcp)")
class TestGetBedrockDailyUsageStats(unittest.TestCase):
    """Test get_bedrock_daily_usage_stats tool."""

    def setUp(self):
        if "server" in sys.modules:
            del sys.modules["server"]

    @patch("boto3.client")
    def test_parses_cloudwatch_log_events(self, mock_boto_client):
        mock_logs = MagicMock()
        mock_logs.describe_log_groups.return_value = {
            "logGroups": [{"logGroupName": "/aws/bedrock/invocation-logs"}]
        }
        mock_logs.filter_log_events.return_value = {
            "events": [
                {
                    "timestamp": 1740700800000,  # 2025-02-28T00:00:00Z
                    "message": json.dumps(
                        {
                            "inputTokenCount": 1000,
                            "outputTokenCount": 500,
                            "modelId": "anthropic.claude-opus-4-6",
                            "timestamp": "2026-02-28T12:00:00Z",
                        }
                    ),
                },
                {
                    "timestamp": 1740700800000,
                    "message": json.dumps(
                        {
                            "inputTokenCount": 2000,
                            "outputTokenCount": 1000,
                            "modelId": "anthropic.claude-opus-4-6",
                            "timestamp": "2026-02-28T14:00:00Z",
                        }
                    ),
                },
            ],
        }
        mock_boto_client.return_value = mock_logs

        import server

        result_str = server.get_bedrock_daily_usage_stats(7)
        result = json.loads(result_str)

        self.assertEqual(result["days_queried"], 7)
        self.assertEqual(result["log_events_processed"], 2)
        self.assertEqual(len(result["daily_stats"]), 1)

        day = result["daily_stats"][0]
        self.assertEqual(day["date"], "2026-02-28")
        self.assertEqual(day["inputTokens"], 3000)
        self.assertEqual(day["outputTokens"], 1500)
        self.assertEqual(day["totalTokens"], 4500)
        self.assertEqual(day["invocations"], 2)
        self.assertIn("anthropic.claude-opus-4-6", day["models"])
        self.assertEqual(day["models"]["anthropic.claude-opus-4-6"], 2)

    @patch("boto3.client")
    def test_handles_missing_log_group(self, mock_boto_client):
        mock_logs = MagicMock()
        mock_logs.describe_log_groups.return_value = {"logGroups": []}
        mock_boto_client.return_value = mock_logs

        import server

        result_str = server.get_bedrock_daily_usage_stats(7)
        result = json.loads(result_str)

        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    @patch("boto3.client")
    def test_handles_malformed_log_entries(self, mock_boto_client):
        mock_logs = MagicMock()
        mock_logs.describe_log_groups.return_value = {
            "logGroups": [{"logGroupName": "/aws/bedrock/invocation-logs"}]
        }
        mock_logs.filter_log_events.return_value = {
            "events": [
                {"timestamp": 1740700800000, "message": "not json"},
                {"timestamp": 1740700800000, "message": "{}"},
                {
                    "timestamp": 1740700800000,
                    "message": json.dumps(
                        {
                            "inputTokenCount": 100,
                            "outputTokenCount": 50,
                            "modelId": "test-model",
                            "timestamp": "2026-02-28T12:00:00Z",
                        }
                    ),
                },
            ],
        }
        mock_boto_client.return_value = mock_logs

        import server

        result_str = server.get_bedrock_daily_usage_stats(7)
        result = json.loads(result_str)

        # Should process the valid entry and skip malformed ones
        self.assertEqual(result["log_events_processed"], 3)
        self.assertEqual(len(result["daily_stats"]), 1)
        self.assertEqual(result["daily_stats"][0]["totalTokens"], 150)

    @patch("boto3.client")
    def test_handles_usage_nested_format(self, mock_boto_client):
        """Some log formats nest token counts under 'usage' key."""
        mock_logs = MagicMock()
        mock_logs.describe_log_groups.return_value = {
            "logGroups": [{"logGroupName": "/aws/bedrock/invocation-logs"}]
        }
        mock_logs.filter_log_events.return_value = {
            "events": [
                {
                    "timestamp": 1740700800000,
                    "message": json.dumps(
                        {
                            "usage": {
                                "inputTokens": 500,
                                "outputTokens": 250,
                            },
                            "modelId": "test-model",
                            "timestamp": "2026-02-28T12:00:00Z",
                        }
                    ),
                },
            ],
        }
        mock_boto_client.return_value = mock_logs

        import server

        result_str = server.get_bedrock_daily_usage_stats(7)
        result = json.loads(result_str)

        self.assertEqual(len(result["daily_stats"]), 1)
        self.assertEqual(result["daily_stats"][0]["inputTokens"], 500)
        self.assertEqual(result["daily_stats"][0]["outputTokens"], 250)


if __name__ == "__main__":
    unittest.main()
