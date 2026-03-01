#!/usr/bin/env python3
"""AWS Cost Explorer MCP Server — provides cost analysis tools via MCP protocol.

Exposes two tools:
  - get_detailed_breakdown_by_day: AWS Cost Explorer daily breakdown by service
  - get_bedrock_daily_usage_stats: Bedrock usage stats from CloudWatch invocation logs

Runs as a stdio MCP server spawned by the Claude Code SDK.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("aws-cost")

REGION = os.environ.get("AWS_REGION", "us-west-2")
LOG_GROUP_NAME = os.environ.get(
    "BEDROCK_LOG_GROUP_NAME", "/aws/bedrock/invocation-logs"
)


@mcp.tool()
def get_detailed_breakdown_by_day(days: int = 7) -> str:
    """Get a detailed AWS Cost Explorer breakdown by service for the last N days.

    Returns daily costs grouped by AWS service, including total cost per service
    and per day. Useful for understanding infrastructure spending patterns.

    Args:
        days: Number of days to look back (default: 7, max: 90)
    """
    ce = boto3.client("ce", region_name=REGION)

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=min(days, 90))).strftime("%Y-%m-%d")

    try:
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["BlendedCost", "UsageQuantity"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
        )

        results = []
        for period in response.get("ResultsByTime", []):
            day_data = {
                "date": period["TimePeriod"]["Start"],
                "services": [],
                "total": 0.0,
            }
            for group in period.get("Groups", []):
                service = group["Keys"][0]
                cost = float(group["Metrics"]["BlendedCost"]["Amount"])
                usage = float(group["Metrics"]["UsageQuantity"]["Amount"])
                if cost > 0.0001 or usage > 0:
                    day_data["services"].append(
                        {
                            "service": service,
                            "cost_usd": round(cost, 6),
                            "usage_quantity": round(usage, 4),
                        }
                    )
                    day_data["total"] += cost

            day_data["total"] = round(day_data["total"], 6)
            day_data["services"].sort(key=lambda x: x["cost_usd"], reverse=True)
            results.append(day_data)

        return json.dumps(results, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_bedrock_daily_usage_stats(days: int = 7) -> str:
    """Get Bedrock model usage statistics from CloudWatch invocation logs.

    Queries CloudWatch Logs for Bedrock invocation records and aggregates
    token usage, invocation counts, and model distribution by day.

    Args:
        days: Number of days to look back (default: 7, max: 30)
    """
    logs_client = boto3.client("logs", region_name=REGION)

    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int(
        (datetime.now(timezone.utc) - timedelta(days=min(days, 30))).timestamp() * 1000
    )

    try:
        # Check if log group exists
        try:
            resp = logs_client.describe_log_groups(
                logGroupNamePrefix=LOG_GROUP_NAME, limit=1
            )
            if not resp.get("logGroups"):
                return json.dumps(
                    {
                        "error": f"Log group {LOG_GROUP_NAME} not found",
                        "hint": "Bedrock invocation logging may not be enabled",
                    }
                )
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Cannot access log group {LOG_GROUP_NAME}: {e}",
                    "hint": "Check IAM permissions for logs:DescribeLogGroups",
                }
            )

        # Query log events
        events = []
        kwargs = {
            "logGroupName": LOG_GROUP_NAME,
            "startTime": start_time,
            "endTime": end_time,
            "limit": 10000,
        }

        while True:
            response = logs_client.filter_log_events(**kwargs)
            events.extend(response.get("events", []))

            next_token = response.get("nextToken")
            if not next_token or len(events) >= 10000:
                break
            kwargs["nextToken"] = next_token

        # Parse and aggregate by day
        by_day = {}
        for event in events:
            try:
                record = json.loads(event.get("message", "{}"))

                # Bedrock Converse API nests tokens under input/output blocks
                input_block = record.get("input", {}) if isinstance(record.get("input"), dict) else {}
                output_block = record.get("output", {}) if isinstance(record.get("output"), dict) else {}

                input_tokens = input_block.get("inputTokenCount", 0) or record.get("inputTokenCount", 0)
                output_tokens = output_block.get("outputTokenCount", 0) or record.get("outputTokenCount", 0)

                # Fallback: output.outputBodyJson.usage
                if not input_tokens and not output_tokens:
                    out_body = output_block.get("outputBodyJson", {})
                    if isinstance(out_body, dict):
                        usage = out_body.get("usage", {})
                    else:
                        usage = {}
                    input_tokens = usage.get("inputTokens", usage.get("input_tokens", 0))
                    output_tokens = usage.get("outputTokens", usage.get("output_tokens", 0))

                # Final fallback: top-level usage block
                if not input_tokens and not output_tokens:
                    usage = record.get("usage", {})
                    if isinstance(usage, dict):
                        input_tokens = usage.get(
                            "inputTokens", usage.get("input_tokens", 0)
                        )
                        output_tokens = usage.get(
                            "outputTokens", usage.get("output_tokens", 0)
                        )

                # Skip entries with no token data
                if not input_tokens and not output_tokens:
                    continue

                model_id = record.get("modelId", record.get("model_id", "unknown"))

                timestamp = record.get("timestamp", "")
                if isinstance(timestamp, (int, float)):
                    date_str = datetime.fromtimestamp(
                        timestamp / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                elif isinstance(timestamp, str) and len(timestamp) >= 10:
                    date_str = timestamp[:10]
                else:
                    date_str = datetime.fromtimestamp(
                        event["timestamp"] / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")

                if date_str not in by_day:
                    by_day[date_str] = {
                        "date": date_str,
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "totalTokens": 0,
                        "invocations": 0,
                        "models": {},
                    }

                by_day[date_str]["inputTokens"] += input_tokens
                by_day[date_str]["outputTokens"] += output_tokens
                by_day[date_str]["totalTokens"] += input_tokens + output_tokens
                by_day[date_str]["invocations"] += 1

                if model_id not in by_day[date_str]["models"]:
                    by_day[date_str]["models"][model_id] = 0
                by_day[date_str]["models"][model_id] += 1

            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        results = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)
        return json.dumps(
            {
                "days_queried": days,
                "log_events_processed": len(events),
                "daily_stats": results,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
