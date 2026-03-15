"""Warm Pool Manager Lambda — maintains pre-warmed AgentCore sessions.

Triggered by EventBridge on a schedule (e.g., every 60s). Checks the warm pool
in DynamoDB and replenishes it to the target size by creating new AgentCore
sessions and sending Phase 1 warmup requests (no userId).

DynamoDB schema (reuses openclaw-identity table):
  PK: WARMPOOL#available  SK: SESSION#{sessionId}
  Attributes: createdAt (ISO), ttl (epoch), runtimeSessionId

Flow:
  1. Query available sessions in warm pool
  2. Remove expired/stale sessions
  3. If count < target → create new sessions + invoke warmup
  4. Log pool status for monitoring
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration from environment
IDENTITY_TABLE_NAME = os.environ.get("IDENTITY_TABLE_NAME", "openclaw-identity")
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
AGENTCORE_QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")
TARGET_POOL_SIZE = int(os.environ.get("TARGET_POOL_SIZE", "1"))
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "25"))
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Clients
dynamodb = boto3.resource("dynamodb", region_name=REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)

agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(read_timeout=600, retries={"max_attempts": 1}),
)


def get_available_sessions():
    """Query all available warm pool sessions from DynamoDB."""
    try:
        resp = identity_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": "WARMPOOL#available",
                ":sk_prefix": "SESSION#",
            },
        )
        return resp.get("Items", [])
    except Exception as e:
        logger.error("Failed to query warm pool: %s", e)
        return []


def remove_session(session_id):
    """Remove a session from the warm pool."""
    try:
        identity_table.delete_item(
            Key={"PK": "WARMPOOL#available", "SK": f"SESSION#{session_id}"},
        )
        logger.info("Removed session %s from warm pool", session_id)
    except Exception as e:
        logger.error("Failed to remove session %s: %s", session_id, e)


def add_session(session_id, runtime_session_id):
    """Add a pre-warmed session to the warm pool."""
    now_epoch = int(time.time())
    ttl_epoch = now_epoch + (SESSION_TTL_MINUTES * 60)
    try:
        identity_table.put_item(
            Item={
                "PK": "WARMPOOL#available",
                "SK": f"SESSION#{session_id}",
                "runtimeSessionId": runtime_session_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "ttl": ttl_epoch,
            },
        )
        logger.info("Added session %s to warm pool (ttl=%s)", session_id, ttl_epoch)
    except Exception as e:
        logger.error("Failed to add session %s: %s", session_id, e)


def warmup_session(runtime_session_id):
    """Send Phase 1 warmup request to AgentCore (no userId = generic pre-warm)."""
    payload = json.dumps({"action": "warmup"}).encode()
    try:
        logger.info(
            "Warming up session %s (arn=%s qualifier=%s)",
            runtime_session_id,
            AGENTCORE_RUNTIME_ARN,
            AGENTCORE_QUALIFIER,
        )
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=runtime_session_id,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
        status_code = resp.get("statusCode")
        body = resp.get("response")
        body_text = ""
        if body:
            if hasattr(body, "read"):
                body_text = body.read().decode("utf-8")
            else:
                body_text = str(body)
        logger.info(
            "Warmup response: status=%s body=%s",
            status_code,
            body_text[:200],
        )
        return True
    except Exception as e:
        logger.error("Warmup failed for session %s: %s", runtime_session_id, e)
        return False


def cleanup_expired(sessions):
    """Remove expired sessions from the pool. Returns count of valid sessions."""
    now_epoch = int(time.time())
    valid = []
    for session in sessions:
        ttl = session.get("ttl", 0)
        if ttl and ttl < now_epoch:
            session_id = session["SK"].replace("SESSION#", "")
            logger.info("Removing expired session %s (ttl=%s, now=%s)", session_id, ttl, now_epoch)
            remove_session(session_id)
        else:
            valid.append(session)
    return valid


def handler(event, context):
    """Main handler — check pool and replenish if needed."""
    logger.info(
        "Warm pool check: target=%d, ttl_minutes=%d, runtime=%s",
        TARGET_POOL_SIZE,
        SESSION_TTL_MINUTES,
        AGENTCORE_RUNTIME_ARN,
    )

    if not AGENTCORE_RUNTIME_ARN:
        logger.error("AGENTCORE_RUNTIME_ARN not set — skipping")
        return {"status": "error", "reason": "missing runtime ARN"}

    # 1. Get current available sessions
    sessions = get_available_sessions()
    logger.info("Current warm pool: %d sessions", len(sessions))

    # 2. Clean up expired
    valid_sessions = cleanup_expired(sessions)
    current_count = len(valid_sessions)
    logger.info("After cleanup: %d valid sessions", current_count)

    # 3. Replenish if needed
    needed = TARGET_POOL_SIZE - current_count
    created = 0
    failed = 0

    if needed > 0:
        logger.info("Need to create %d new sessions", needed)
        for i in range(needed):
            # AgentCore requires runtimeSessionId >= 33 chars
            long_id = uuid.uuid4().hex + uuid.uuid4().hex[:9]
            runtime_session_id = f"warmpool_{long_id}"
            logger.info("Creating warm session %d/%d: %s", i + 1, needed, runtime_session_id)

            success = warmup_session(runtime_session_id)
            if success:
                add_session(long_id, runtime_session_id)
                created += 1
            else:
                failed += 1
    else:
        logger.info("Pool is full (%d/%d), no action needed", current_count, TARGET_POOL_SIZE)

    result = {
        "status": "ok",
        "pool_before": len(sessions),
        "pool_after_cleanup": current_count,
        "target": TARGET_POOL_SIZE,
        "created": created,
        "failed": failed,
        "pool_final": current_count + created,
    }
    logger.info("Warm pool result: %s", json.dumps(result))
    return result
