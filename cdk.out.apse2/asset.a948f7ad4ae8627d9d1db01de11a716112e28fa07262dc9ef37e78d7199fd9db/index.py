"""AgentCore Runtime Keepalive Lambda.

Invoked every 5 minutes by EventBridge to ensure the OpenClaw container
is running on AgentCore Runtime. On first invocation, starts a new session.
On subsequent invocations, sends a keepalive ping to the existing session.
If the session has been terminated (8-hour max lifetime), starts a new one.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "openclaw_agent_live")
# Session ID must be >= 33 characters
SESSION_ID = os.environ.get(
    "SESSION_ID",
    "openclaw-telegram-session-primary-keepalive-001",
)

agentcore_client = boto3.client("bedrock-agentcore")


def handler(event, context):
    """Invoke the AgentCore Runtime to start or keep alive the OpenClaw session."""
    logger.info(
        "Keepalive invocation: runtime=%s qualifier=%s session=%s",
        RUNTIME_ARN,
        QUALIFIER,
        SESSION_ID,
    )

    try:
        payload = json.dumps({"action": "keepalive"}).encode()

        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            qualifier=QUALIFIER,
            runtimeSessionId=SESSION_ID,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )

        # Read the response body
        body = response.get("response")
        if body:
            if hasattr(body, "read"):
                body_text = body.read().decode("utf-8")
            else:
                body_text = str(body)
        else:
            body_text = "{}"

        logger.info("Keepalive response: %s", body_text[:500])

        return {
            "statusCode": 200,
            "body": body_text,
            "runtimeSessionId": response.get("runtimeSessionId", SESSION_ID),
        }

    except agentcore_client.exceptions.ResourceNotFoundException:
        logger.warning("Session not found — will be created on next invocation")
        return {"statusCode": 404, "body": "Session not found"}

    except Exception as e:
        logger.error("Keepalive failed: %s", str(e), exc_info=True)
        return {"statusCode": 500, "body": str(e)}
