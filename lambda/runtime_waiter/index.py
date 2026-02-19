"""CloudFormation custom resource handler that waits for an AgentCore Runtime
to reach READY status before allowing dependent resources (e.g. RuntimeEndpoint)
to be created.

Used by the CDK Provider framework — the on_event handler polls GetRuntime in a
loop with sleep, so no is_complete handler or Step Functions state machine is needed.
"""

import json
import logging
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

POLL_INTERVAL_SECONDS = 30
MAX_ATTEMPTS = 30  # 30 * 30s = 15 minutes


def on_event(event, context):
    logger.info("Received event: %s", json.dumps(event))

    request_type = event["RequestType"]
    runtime_id = event["ResourceProperties"]["AgentRuntimeId"]

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", runtime_id)}

    # Create or Update: poll until READY
    client = boto3.client("bedrock-agentcore")
    status = "UNKNOWN"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.get_runtime(agentRuntimeId=runtime_id)
        status = response.get("status", "UNKNOWN")
        logger.info(
            "Attempt %d/%d: Runtime %s status: %s",
            attempt, MAX_ATTEMPTS, runtime_id, status,
        )

        if status == "READY":
            return {"PhysicalResourceId": runtime_id}

        if status in ("FAILED", "DELETE_FAILED"):
            raise Exception(f"Runtime {runtime_id} entered {status} status")

        if attempt < MAX_ATTEMPTS:
            time.sleep(POLL_INTERVAL_SECONDS)

    raise Exception(
        f"Runtime {runtime_id} did not reach READY status within "
        f"{MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s (last status: {status})"
    )
