"""Token Metrics Lambda — processes Bedrock invocation logs.

Triggered by CloudWatch Logs subscription filter on /aws/bedrock/invocation-logs.
For each invocation log entry:
  1. Extracts token counts (input/output), model ID
  2. Extracts OpenClaw metadata (actor_id, session_id, channel) from request metadata
  3. Computes estimated cost using model pricing lookup
  4. Writes record to DynamoDB with composite keys for multi-access patterns
  5. Publishes CloudWatch custom metrics with dimensions
"""

import base64
import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
TABLE_NAME = os.environ["TABLE_NAME"]
TTL_DAYS = int(os.environ.get("TTL_DAYS", "90"))
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "OpenClaw/TokenUsage")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
cloudwatch = boto3.client("cloudwatch")

# --- Model pricing (per 1M tokens) ---
MODEL_PRICING = {
    # Amazon Nova
    "amazon.nova-2-lite-v1:0": {"input": 0.30, "output": 2.50},
    "global.amazon.nova-2-lite-v1:0": {"input": 0.30, "output": 2.50},
    "amazon.nova-pro-v1:0": {"input": 0.80, "output": 3.20},
    "amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14},
    # Claude
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.25, "output": 1.25},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 0.80, "output": 4.00},
    "anthropic.claude-sonnet-4-20250514-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}

# Default pricing when model is unknown
DEFAULT_PRICING = {"input": 1.00, "output": 5.00}


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD based on model pricing."""
    pricing = DEFAULT_PRICING
    for key, price in MODEL_PRICING.items():
        if key in model_id:
            pricing = price
            break

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 8)


def extract_openclaw_metadata(log_entry: dict) -> dict:
    """Extract OpenClaw metadata (actor_id, session_id, channel) from the log entry.

    Bedrock invocation logs may contain request metadata or we can correlate
    via X-Ray trace IDs. Supports both direct Bedrock and AgentCore log formats.
    """
    metadata = {
        "actor_id": "default-user",
        "session_id": "default-session",
        "channel": "unknown",
    }

    # Try to extract from request metadata or headers
    request_metadata = log_entry.get("requestMetadata", {})
    if request_metadata:
        metadata["actor_id"] = request_metadata.get("openclaw.actor_id", metadata["actor_id"])
        metadata["session_id"] = request_metadata.get("openclaw.session_id", metadata["session_id"])
        metadata["channel"] = request_metadata.get("openclaw.channel", metadata["channel"])

    # AgentCore logs: extract from sessionState.promptSessionAttributes
    session_state = log_entry.get("sessionState", {})
    prompt_attrs = session_state.get("promptSessionAttributes", {})
    if prompt_attrs:
        metadata["actor_id"] = prompt_attrs.get("actor_id", metadata["actor_id"])
        metadata["channel"] = prompt_attrs.get("channel", metadata["channel"])

    # AgentCore logs: extract session ID from top-level field
    if log_entry.get("sessionId"):
        metadata["session_id"] = log_entry["sessionId"]

    # AgentCore logs: extract from agentRuntime metadata
    runtime_metadata = log_entry.get("agentRuntimeMetadata", {})
    if runtime_metadata:
        metadata["actor_id"] = runtime_metadata.get("actorId", metadata["actor_id"])
        metadata["session_id"] = runtime_metadata.get("sessionId", metadata["session_id"])

    # Also check the input body for custom attributes
    input_body = log_entry.get("input", {})
    if isinstance(input_body, str):
        try:
            input_body = json.loads(input_body)
        except (json.JSONDecodeError, TypeError):
            input_body = {}

    # Some models embed metadata in the request
    custom_attrs = input_body.get("metadata", {})
    if custom_attrs:
        metadata["actor_id"] = custom_attrs.get("actor_id", metadata["actor_id"])
        metadata["session_id"] = custom_attrs.get("session_id", metadata["session_id"])
        metadata["channel"] = custom_attrs.get("channel", metadata["channel"])

    return metadata


def write_to_dynamodb(record: dict):
    """Write a token usage record to DynamoDB with composite keys."""
    actor_id = record["actor_id"]
    session_id = record["session_id"]
    channel = record["channel"]
    model_id = record["model_id"]
    date_str = record["date"]
    cost = record["estimated_cost_usd"]

    # TTL: current time + TTL_DAYS
    ttl = int(time.time()) + (TTL_DAYS * 86400)

    # Zero-pad cost for lexicographic sort in GSI3
    cost_sort = f"{cost:015.8f}"

    item = {
        "PK": f"USER#{actor_id}",
        "SK": f"DATE#{date_str}#CHANNEL#{channel}#SESSION#{session_id}",
        # GSI1: Channel aggregation
        "GSI1PK": f"CHANNEL#{channel}",
        "GSI1SK": f"DATE#{date_str}",
        # GSI2: Model aggregation
        "GSI2PK": f"MODEL#{model_id}",
        "GSI2SK": f"DATE#{date_str}",
        # GSI3: Daily cost ranking
        "GSI3PK": f"DATE#{date_str}",
        "GSI3SK": f"COST#{cost_sort}",
        # Attributes
        "inputTokens": record["input_tokens"],
        "outputTokens": record["output_tokens"],
        "totalTokens": record["total_tokens"],
        "estimatedCostUSD": str(cost),
        "invocationCount": 1,
        "channel": channel,
        "modelId": model_id,
        "actorId": actor_id,
        "sessionId": session_id,
        "timestamp": record["timestamp"],
        "ttl": ttl,
    }

    # Use update expression to accumulate if record already exists
    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression=(
            "SET GSI1PK = :g1pk, GSI1SK = :g1sk, "
            "GSI2PK = :g2pk, GSI2SK = :g2sk, "
            "GSI3PK = :g3pk, GSI3SK = :g3sk, "
            "channel = :channel, modelId = :model, "
            "actorId = :actor, sessionId = :session, "
            "#ts = :ts, #ttl_attr = :ttl "
            "ADD inputTokens :inp, outputTokens :out, "
            "totalTokens :total, invocationCount :one"
        ),
        ExpressionAttributeNames={
            "#ts": "timestamp",
            "#ttl_attr": "ttl",
        },
        ExpressionAttributeValues={
            ":g1pk": item["GSI1PK"],
            ":g1sk": item["GSI1SK"],
            ":g2pk": item["GSI2PK"],
            ":g2sk": item["GSI2SK"],
            ":g3pk": item["GSI3PK"],
            ":g3sk": item["GSI3SK"],
            ":channel": channel,
            ":model": model_id,
            ":actor": actor_id,
            ":session": session_id,
            ":ts": record["timestamp"],
            ":ttl": ttl,
            ":inp": record["input_tokens"],
            ":out": record["output_tokens"],
            ":total": record["total_tokens"],
            ":one": 1,
        },
    )


def publish_metrics(record: dict):
    """Publish CloudWatch custom metrics with dimensions."""
    dimensions_base = [
        {"Name": "ActorId", "Value": record["actor_id"]},
        {"Name": "Channel", "Value": record["channel"]},
        {"Name": "ModelId", "Value": record["model_id"]},
    ]

    # Also publish without dimensions for aggregate view
    metric_data = []
    timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))

    for dims in [dimensions_base, []]:
        metric_data.extend(
            [
                {
                    "MetricName": "InputTokens",
                    "Dimensions": dims,
                    "Timestamp": timestamp,
                    "Value": record["input_tokens"],
                    "Unit": "Count",
                },
                {
                    "MetricName": "OutputTokens",
                    "Dimensions": dims,
                    "Timestamp": timestamp,
                    "Value": record["output_tokens"],
                    "Unit": "Count",
                },
                {
                    "MetricName": "TotalTokens",
                    "Dimensions": dims,
                    "Timestamp": timestamp,
                    "Value": record["total_tokens"],
                    "Unit": "Count",
                },
                {
                    "MetricName": "EstimatedCostUSD",
                    "Dimensions": dims,
                    "Timestamp": timestamp,
                    "Value": record["estimated_cost_usd"],
                    "Unit": "None",
                },
                {
                    "MetricName": "InvocationCount",
                    "Dimensions": dims,
                    "Timestamp": timestamp,
                    "Value": 1,
                    "Unit": "Count",
                },
            ]
        )

    # CloudWatch PutMetricData accepts max 1000 metric data points per call
    # Batch into groups of 25 (well under the limit)
    for i in range(0, len(metric_data), 25):
        batch = metric_data[i : i + 25]
        cloudwatch.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=batch)


def process_log_entry(log_entry: dict):
    """Process a single Bedrock invocation log entry."""
    # Extract token counts
    input_tokens = log_entry.get("inputTokenCount", 0)
    output_tokens = log_entry.get("outputTokenCount", 0)

    # Some log formats nest token counts differently
    if not input_tokens and not output_tokens:
        usage = log_entry.get("usage", {})
        input_tokens = usage.get("inputTokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("outputTokens", usage.get("output_tokens", 0))

    total_tokens = input_tokens + output_tokens

    if total_tokens == 0:
        logger.info("Skipping log entry with zero tokens")
        return

    # Extract model ID
    model_id = log_entry.get("modelId", log_entry.get("model_id", "unknown"))

    # Extract OpenClaw metadata
    metadata = extract_openclaw_metadata(log_entry)

    # Compute timestamp and date
    timestamp = log_entry.get("timestamp", datetime.now(timezone.utc).isoformat())
    if isinstance(timestamp, (int, float)):
        timestamp = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
    date_str = timestamp[:10]  # yyyy-mm-dd

    # Compute estimated cost
    cost = estimate_cost(model_id, input_tokens, output_tokens)

    record = {
        "actor_id": metadata["actor_id"],
        "session_id": metadata["session_id"],
        "channel": metadata["channel"],
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
        "timestamp": timestamp,
        "date": date_str,
    }

    logger.info(
        "Processing invocation: model=%s input=%d output=%d cost=$%.6f actor=%s channel=%s",
        model_id,
        input_tokens,
        output_tokens,
        cost,
        metadata["actor_id"],
        metadata["channel"],
    )

    write_to_dynamodb(record)
    publish_metrics(record)


def handler(event, context):
    """Lambda handler — triggered by CloudWatch Logs subscription filter.

    The event payload is base64-encoded, gzip-compressed CloudWatch Logs data.
    """
    # Decode and decompress the CloudWatch Logs payload
    compressed = base64.b64decode(event["awslogs"]["data"])
    payload = json.loads(gzip.decompress(compressed))

    log_events = payload.get("logEvents", [])
    logger.info("Processing %d log events from %s", len(log_events), payload.get("logGroup", ""))

    processed = 0
    errors = 0

    for log_event in log_events:
        try:
            message = log_event.get("message", "")
            if not message:
                continue

            # Parse the JSON log entry
            try:
                log_entry = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("Skipping non-JSON log entry: %s", message[:200])
                continue

            process_log_entry(log_entry)
            processed += 1

        except Exception:
            errors += 1
            logger.exception("Failed to process log event")

    logger.info("Completed: processed=%d errors=%d total=%d", processed, errors, len(log_events))

    return {
        "processed": processed,
        "errors": errors,
        "total": len(log_events),
    }
