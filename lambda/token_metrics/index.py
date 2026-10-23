"""Token Metrics Lambda — processes Bedrock invocation logs.

Triggered by CloudWatch Logs subscription filter on /aws/bedrock/invocation-logs.
For each invocation log entry:
  1. Extracts token counts (input/output), model ID
  2. Extracts OpenClaw metadata (actor_id, session_id, channel, agent_type) from
     the requestMetadata the bridge proxy sets on every Converse/ConverseStream call
  3. Computes estimated cost using model pricing lookup
  4. Writes record to DynamoDB with composite keys for multi-access patterns
  5. Publishes CloudWatch custom metrics with dimensions
"""

import base64
import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

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
# Keys are matched as substrings of the model ID, first match wins, so a
# prefixed key (e.g. "global.") must come before its unprefixed base key.
# The base key also covers geo profiles (au., us., eu., apac.).
# Optional "cache_read" / "cache_write" are the prompt-cache rates (5-minute
# TTL write). A model without them bills cache tokens at its input rate.
MODEL_PRICING = {
    # Amazon Nova
    "global.amazon.nova-2-lite-v1:0": {"input": 0.30, "output": 2.50},
    "amazon.nova-2-lite-v1:0": {"input": 0.30, "output": 2.50},
    "amazon.nova-pro-v1:0": {"input": 0.80, "output": 3.20},
    "amazon.nova-micro-v1:0": {"input": 0.035, "output": 0.14},
    # Claude
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.25, "output": 1.25},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 0.80, "output": 4.00},
    "anthropic.claude-sonnet-4-20250514-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # Claude Opus 4.6 / 5.5, Sonnet 5.5: AWS Price List bulk offer AmazonBedrockFoundationModels,
    # publication 2026-10-12T08:40:08Z, identical in us-east-1 and ap-southeast-2.
    # The global cross-Region profile is cheaper than geo / in-Region.
    # Opus 4.6 usagetypes <R>-MP:<R>_{InputTokenCount,OutputTokenCount,
    # CacheReadInputTokenCount,CacheWriteInputTokenCount}[_Global]-Units
    # (us-east-1 global SKUs MTYB4GKWBMZQVSPV, K8TYGUKS5EQFRM3F, KXCKESFRTF63AAC8,
    # KTKDKQ27XHRK5GXF; regional GZG423Y4VEBAMFMP, HTCQM6TSYSHDEY34, H9AHG8JC5XET4BBQ,
    # J3BFGREX5X4DS4TK).
    "global.anthropic.claude-opus-4-6": {
        "input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25,
    },
    "anthropic.claude-opus-4-6": {
        "input": 5.50, "output": 27.50, "cache_read": 0.55, "cache_write": 6.875,
    },
    # Opus 5.5 usagetypes <R>-MP:<R>_{input_tokens,output_tokens,cache_read_tokens,
    # cache_write_tokens}_[global_]standard-Units (us-east-1 global SKUs
    # KVG5FBPDJPKF5TJY, MWH4TD2A4D5CEBAP, QT3QZUNUXV325ZDF, RVG5HJ3PZEQYVC74;
    # standard J9QFZT8WAQABG9ZX, FCRGDQ596BG7EQKH, E5RCDXA3KP96TBAZ, WXH7FJ56PSA2AT5S).
    "global.anthropic.claude-opus-5-5": {
        "input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00,
    },
    "anthropic.claude-opus-5-5": {
        "input": 4.40, "output": 22.00, "cache_read": 0.22, "cache_write": 5.50,
    },
    # Sonnet 5.5: same offer and publication, identical in us-east-1, us-west-2 and
    # ap-southeast-2. Usagetypes as Opus 5.5 (us-east-1 global SKUs
    # PXMCKMF8EGSRB3GB, HP32J6RXJZ3XYFUM, 487WBAJQRFHYFE5M, CES7A5M9YH89UF9G;
    # standard 6TG78WT6WYJUVS72, ZNY2E4B6MTPHYXVH, H6MJYZQS5DCKBCCM, 99NPNFEPJP2AB79S).
    "global.anthropic.claude-sonnet-5-5": {
        "input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50,
    },
    "anthropic.claude-sonnet-5-5": {
        "input": 2.20, "output": 11.00, "cache_read": 0.11, "cache_write": 2.75,
    },
    # Claude Haiku 5.5 (standard context), same global/geo split
    # (AWS Price List, AmazonBedrockFoundationModels us-east-1, 2026-10-14).
    "global.anthropic.claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "anthropic.claude-haiku-5-5": {"input": 0.11, "output": 0.55},
    "minimax.minimax-m2": {"input": 1.00, "output": 5.00},
}

# Default pricing when model is unknown
DEFAULT_PRICING = {"input": 1.00, "output": 5.00}

# Unknown model IDs already warned about, so each is logged once per container.
_warned_unknown_models = set()

# Stored cost precision: the 8 decimal places estimate_cost() rounds to.
COST_QUANTUM = Decimal("0.00000001")
# GSI3SK is "COST#" + a fixed-width 015.8f total so string order = numeric
# order. Totals above this cap share the top sort key instead of breaking order.
MAX_SORTABLE_COST = Decimal("999999.99999999")


def _lookup_pricing(model_id: str) -> dict:
    for key, price in MODEL_PRICING.items():
        if key in model_id:
            return price
    if model_id not in _warned_unknown_models:
        _warned_unknown_models.add(model_id)
        logger.warning("No pricing for model %s; using default pricing", model_id)
    return DEFAULT_PRICING


def estimate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate cost in USD based on model pricing."""
    pricing = _lookup_pricing(model_id)

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    cache_read_cost = (cache_read_tokens / 1_000_000) * pricing.get("cache_read", pricing["input"])
    cache_write_cost = (cache_write_tokens / 1_000_000) * pricing.get("cache_write", pricing["input"])
    return round(input_cost + output_cost + cache_read_cost + cache_write_cost, 8)


def cost_sort_key(total: Decimal) -> str:
    """GSI3SK for a cumulative cost: fixed width, so string sort = numeric sort."""
    if total > MAX_SORTABLE_COST:
        logger.warning("Cost total %s exceeds the GSI3SK width; capping the sort key", total)
        total = MAX_SORTABLE_COST
    return f"COST#{total.quantize(COST_QUANTUM):015.8f}"


def _int_field(source, *names) -> int:
    if not isinstance(source, dict):
        return 0
    for name in names:
        value = source.get(name)
        if isinstance(value, int) and value > 0:
            return value
    return 0


# Attribution values become DynamoDB key parts and CloudWatch dimensions, so
# only short identifier strings are accepted; anything else keeps the default.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9:_@$#=/+,.-]{1,128}$")
_SAFE_CHANNEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AGENT_TYPES = ("main", "subagent", "cron")
UNKNOWN_AGENT_TYPE = "unknown"
UNBOUND_ACTOR = "default-user"


def _clean(value, pattern=_SAFE_VALUE):
    return value if isinstance(value, str) and pattern.match(value) else None


def _as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def extract_openclaw_metadata(log_entry: dict) -> dict:
    """Extract OpenClaw attribution (actor, session, channel, agent type) from a log entry.

    The bridge proxy sets Bedrock ``requestMetadata`` on every Converse /
    ConverseStream call (bridge/request-metadata.js) and Bedrock copies it into
    the invocation log record. That is the authoritative source. Records
    written before the proxy set it, or by any other caller, have none and keep
    the explicit defaults: default-user / default-session / unknown, with
    agent type "unknown". The older AgentCore-shaped fields are still read, but
    only to fill values requestMetadata does not provide.
    """
    metadata = {
        "actor_id": UNBOUND_ACTOR,
        "session_id": "default-session",
        "channel": "unknown",
        "agent_type": UNKNOWN_AGENT_TYPE,
        "attribution": "none",
    }

    def fill(actor=None, session=None, channel=None):
        if _clean(actor):
            metadata["actor_id"] = actor
        if _clean(session):
            metadata["session_id"] = session
        if _clean(channel, _SAFE_CHANNEL):
            metadata["channel"] = channel

    # Legacy shapes first, so requestMetadata below overrides them.
    prompt_attrs = _as_dict(_as_dict(log_entry.get("sessionState")).get("promptSessionAttributes"))
    if prompt_attrs:
        fill(prompt_attrs.get("actor_id"), None, prompt_attrs.get("channel"))
        metadata["attribution"] = "legacy"
    if log_entry.get("sessionId"):
        fill(session=log_entry.get("sessionId"))
        metadata["attribution"] = "legacy"
    runtime_metadata = _as_dict(log_entry.get("agentRuntimeMetadata"))
    if runtime_metadata:
        fill(runtime_metadata.get("actorId"), runtime_metadata.get("sessionId"))
        metadata["attribution"] = "legacy"
    custom_attrs = _as_dict(_as_dict(log_entry.get("input")).get("metadata"))
    if custom_attrs:
        fill(custom_attrs.get("actor_id"), custom_attrs.get("session_id"), custom_attrs.get("channel"))
        metadata["attribution"] = "legacy"

    request_metadata = _as_dict(log_entry.get("requestMetadata"))
    if request_metadata.get("openclaw.actor_id") is not None:
        fill(request_metadata.get("openclaw.actor_id"),
             request_metadata.get("openclaw.session_id"),
             request_metadata.get("openclaw.channel"))
        agent_type = request_metadata.get("openclaw.agent_type")
        metadata["agent_type"] = agent_type if agent_type in AGENT_TYPES else UNKNOWN_AGENT_TYPE
        metadata["attribution"] = "request-metadata"

    return metadata


def write_to_dynamodb(record: dict):
    """Accumulate a token usage record into its DynamoDB row.

    One row per user/day/channel/session holds running totals; a record with a
    known agent type (main / subagent / cron) gets its own row per agent type,
    so a record without one (written before the proxy set requestMetadata)
    keeps accumulating into the pre-existing row with the old sort key. The first
    update ADDs tokens and cost atomically and returns the new cost total;
    the second sets GSI3SK from that total, conditional on the total being
    unchanged, so a concurrent writer that added more cost (and will set
    GSI3SK from its own, higher total) is never overwritten with a lower one.
    """
    actor_id = record["actor_id"]
    session_id = record["session_id"]
    channel = record["channel"]
    model_id = record["model_id"]
    date_str = record["date"]
    agent_type = record.get("agent_type", UNKNOWN_AGENT_TYPE)
    cost = Decimal(str(record["estimated_cost_usd"])).quantize(COST_QUANTUM)

    # TTL: current time + TTL_DAYS
    ttl = int(time.time()) + (TTL_DAYS * 86400)

    sort_key = f"DATE#{date_str}#CHANNEL#{channel}#SESSION#{session_id}"
    if agent_type in AGENT_TYPES:
        sort_key += f"#AGENT#{agent_type}"
    key = {"PK": f"USER#{actor_id}", "SK": sort_key}

    response = table.update_item(
        Key=key,
        UpdateExpression=(
            "SET GSI1PK = :g1pk, GSI1SK = :g1sk, "
            "GSI2PK = :g2pk, GSI2SK = :g2sk, "
            # A new row is indexed at once (its total is this call's cost);
            # an existing row's GSI3SK is set from the total below.
            "GSI3PK = :g3pk, GSI3SK = if_not_exists(GSI3SK, :g3sk), "
            "channel = :channel, modelId = :model, "
            "actorId = :actor, sessionId = :session, "
            "agentType = :agent, attribution = :attribution, "
            "#ts = :ts, #ttl_attr = :ttl "
            "ADD inputTokens :inp, outputTokens :out, "
            "totalTokens :total, invocationCount :one, "
            "estimatedCostUSD :cost"
        ),
        ExpressionAttributeNames={
            "#ts": "timestamp",
            "#ttl_attr": "ttl",
        },
        ExpressionAttributeValues={
            # GSI1: Channel aggregation
            ":g1pk": f"CHANNEL#{channel}",
            ":g1sk": f"DATE#{date_str}",
            # GSI2: Model aggregation
            ":g2pk": f"MODEL#{model_id}",
            ":g2sk": f"DATE#{date_str}",
            # GSI3: Daily cost ranking
            ":g3pk": f"DATE#{date_str}",
            ":g3sk": cost_sort_key(cost),
            ":channel": channel,
            ":model": model_id,
            ":actor": actor_id,
            ":session": session_id,
            ":agent": agent_type,
            ":attribution": record.get("attribution", "none"),
            ":ts": record["timestamp"],
            ":ttl": ttl,
            ":inp": record["input_tokens"],
            ":out": record["output_tokens"],
            ":total": record["total_tokens"],
            ":one": 1,
            ":cost": cost,
        },
        ReturnValues="UPDATED_NEW",
    )
    total = response["Attributes"]["estimatedCostUSD"]

    try:
        table.update_item(
            Key=key,
            UpdateExpression="SET GSI3SK = :g3sk",
            ConditionExpression="estimatedCostUSD = :total",
            ExpressionAttributeValues={":g3sk": cost_sort_key(total), ":total": total},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # A concurrent write added cost after ours and sets GSI3SK from its total.
        logger.warning("GSI3SK update skipped: cost total moved past %s concurrently", total)


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
    # Bedrock ModelInvocationLog nests counts under input / output; older or
    # flattened records carry them at the top level.
    log_input = log_entry.get("input") if isinstance(log_entry.get("input"), dict) else {}
    log_output = log_entry.get("output") if isinstance(log_entry.get("output"), dict) else {}
    input_tokens = _int_field(log_input, "inputTokenCount") or _int_field(log_entry, "inputTokenCount")
    output_tokens = _int_field(log_output, "outputTokenCount") or _int_field(log_entry, "outputTokenCount")

    # Some log formats nest token counts differently
    usage = log_entry.get("usage") if isinstance(log_entry.get("usage"), dict) else {}
    body = log_output.get("outputBodyJson")
    if not usage and isinstance(body, dict) and isinstance(body.get("usage"), dict):
        usage = body["usage"]  # Converse TokenUsage
    if not input_tokens and not output_tokens:
        input_tokens = _int_field(usage, "inputTokens", "input_tokens")
        output_tokens = _int_field(usage, "outputTokens", "output_tokens")

    # Prompt-cache tokens are billed separately from input tokens.
    cache_read_tokens = (_int_field(log_input, "cacheReadInputTokenCount")
                         or _int_field(log_entry, "cacheReadInputTokenCount")
                         or _int_field(usage, "cacheReadInputTokens"))
    cache_write_tokens = (_int_field(log_input, "cacheWriteInputTokenCount")
                          or _int_field(log_entry, "cacheWriteInputTokenCount")
                          or _int_field(usage, "cacheWriteInputTokens"))

    total_tokens = input_tokens + output_tokens

    if total_tokens == 0 and not cache_read_tokens and not cache_write_tokens:
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
    cost = estimate_cost(model_id, input_tokens, output_tokens,
                         cache_read_tokens, cache_write_tokens)

    record = {
        "actor_id": metadata["actor_id"],
        "session_id": metadata["session_id"],
        "channel": metadata["channel"],
        "agent_type": metadata["agent_type"],
        "attribution": metadata["attribution"],
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
        "timestamp": timestamp,
        "date": date_str,
    }

    logger.info(
        "Processing invocation: model=%s input=%d output=%d cost=$%.6f actor=%s channel=%s "
        "agent=%s attribution=%s",
        model_id,
        input_tokens,
        output_tokens,
        cost,
        metadata["actor_id"],
        metadata["channel"],
        metadata["agent_type"],
        metadata["attribution"],
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

            # Parse the JSON log entry. Log only its size on failure: the
            # message can carry prompt or completion text.
            try:
                log_entry = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("Skipping non-JSON log entry (len=%d)", len(message))
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
