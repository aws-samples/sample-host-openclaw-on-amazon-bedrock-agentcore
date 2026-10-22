#!/usr/bin/env python3
"""Translate foundation outputs into AgentCore Runtime V2 configuration overrides.

Reads exported foundation values and session limits to build the execution
role, VPC, lifecycle, and container environment settings. Missing optional
values preserve inherited settings; explicitly empty values request removal.
The CLI reads these inputs from the environment and writes JSON to stdout,
with the environment size diagnostic on stderr.
"""

import json
import os
import sys

from env_suffix import with_suffix
from runtime_deploy import V2_ENV_LIMIT, environment_size


def foundation_configuration(values):
    required = (
        "EXECUTION_ROLE_ARN", "SECURITY_GROUP_ID", "PRIVATE_SUBNET_IDS", "USER_FILES_BUCKET",
        "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "CMK_ARN", "REGION", "ACCOUNT",
    )
    missing = [key for key in required if not values.get(key) or values[key] == "None"]
    if missing:
        raise ValueError("Missing foundation outputs: " + ", ".join(missing))
    mapping = {
        "AWS_REGION": "REGION", "BEDROCK_MODEL_ID": "DEFAULT_MODEL_ID",
        "BEDROCK_FALLBACK_MODEL_ID": "FALLBACK_MODEL_ID",
        "GATEWAY_TOKEN_SECRET_ID": "GATEWAY_TOKEN_SECRET_ID",
        "COGNITO_USER_POOL_ID": "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID": "COGNITO_CLIENT_ID",
        "COGNITO_PASSWORD_SECRET_ID": "COGNITO_PASSWORD_SECRET_ID",
        "S3_USER_FILES_BUCKET": "USER_FILES_BUCKET", "WORKSPACE_SYNC_INTERVAL_MS": "WORKSPACE_SYNC_MS",
        "WORKSPACE_RESTORE_WAIT_MS": "WORKSPACE_RESTORE_WAIT_MS", "IMAGE_VERSION": "IMAGE_VERSION",
        "EXECUTION_ROLE_ARN": "EXECUTION_ROLE_ARN", "CMK_ARN": "CMK_ARN",
        "CRON_LEAD_TIME_MINUTES": "CRON_LEAD_TIME", "SUBAGENT_BEDROCK_MODEL_ID": "SUBAGENT_MODEL_ID",
        "BEDROCK_USER_SELECTABLE_MODEL_IDS": "USER_SELECTABLE_MODEL_IDS",
        "TELEGRAM_CHANNEL_SECRET_ID": "TELEGRAM_CHANNEL_SECRET_ID",
        "BEDROCK_GUARDRAIL_ID": "GUARDRAIL_ID", "BEDROCK_GUARDRAIL_VERSION": "GUARDRAIL_VERSION",
        "BROWSER_IDENTIFIER": "BROWSER_IDENTIFIER", "AGENTCORE_GATEWAY_URL": "AGENTCORE_GATEWAY_URL",
    }
    environment = {target: values[source] for target, source in mapping.items() if values.get(source)}
    # Names created by OpenClawCron/OpenClawRouter, with the same optional
    # environment suffix (OPENCLAW_ENV_SUFFIX; empty keeps the names unchanged).
    cron_function = with_suffix("openclaw-cron-executor", values)
    scheduler_role = with_suffix(f"openclaw-cron-scheduler-role-{values['REGION']}", values)
    environment.update({
        "EVENTBRIDGE_SCHEDULE_GROUP": with_suffix("openclaw-cron", values),
        "CRON_LAMBDA_ARN": f"arn:aws:lambda:{values['REGION']}:{values['ACCOUNT']}:function:{cron_function}",
        "EVENTBRIDGE_ROLE_ARN": f"arn:aws:iam::{values['ACCOUNT']}:role/{scheduler_role}",
        "IDENTITY_TABLE_NAME": with_suffix("openclaw-identity", values),
    })
    configuration = {
        "roleArn": values["EXECUTION_ROLE_ARN"],
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": values["PRIVATE_SUBNET_IDS"].split(","),
                "securityGroups": values["SECURITY_GROUP_ID"].split(","),
            },
        },
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": int(values["SESSION_IDLE"]),
            "maxLifetime": int(values["SESSION_MAX"]),
        },
        "environmentVariables": environment,
    }
    # Missing input preserves inherited settings; explicit empty values disable them.
    unsets = [target for target, source in mapping.items() if source in values and not values[source]]
    if unsets:
        configuration["unsetEnvironmentVariables"] = unsets
    return configuration


if __name__ == "__main__":
    configuration = foundation_configuration(os.environ)
    size = environment_size(configuration["environmentVariables"])
    print(f"Foundation environment: {size} UTF-8 bytes before platform/inherited values; V2 limit {V2_ENV_LIMIT}",
          file=sys.stderr)
    print(json.dumps(configuration))
