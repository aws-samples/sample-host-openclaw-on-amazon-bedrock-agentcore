#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: Per-user AgentCore Runtime sessions with webhook-based
channel ingestion via Router Lambda. No keepalive needed — sessions
idle-terminate naturally.

Hybrid deployment model:
  Phase 1 (CDK): VPC, Security, AgentCore-base (Role/SG/S3), Observability
  Phase 2 (Starter Toolkit): Runtime, Endpoint, ECR, Docker build
  Phase 3 (CDK): Router, Cron tombstone, TokenMonitoring
"""

import os

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.cron_stack import CronStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

REQUIRED_REGION = "eu-west-1"


app = cdk.App()

context_region = app.node.try_get_context("region")
for source, configured_region in (
    ("cdk context region", context_region),
    ("CDK_DEFAULT_REGION", os.environ.get("CDK_DEFAULT_REGION")),
    ("AWS_REGION", os.environ.get("AWS_REGION")),
    ("AWS_DEFAULT_REGION", os.environ.get("AWS_DEFAULT_REGION")),
):
    if configured_region and configured_region != REQUIRED_REGION:
        raise RuntimeError(
            f"{source} must be exactly {REQUIRED_REGION}; got {configured_region}"
        )

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=REQUIRED_REGION,
)

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- Guardrails (Bedrock content filtering — opt-in via enable_guardrails) ---
guardrails_stack = GuardrailsStack(
    app,
    "OpenClawGuardrails",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- AgentCore base resources (Role, SG, S3) ---
# Runtime/Endpoint created by Starter Toolkit; runtime_id/endpoint_id
# injected via cdk.json context after `agentcore deploy`.
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    env=env,
)

# --- Router (Lambda + API Gateway HTTP API for Telegram/Slack webhooks) ---
router_stack = RouterStack(
    app,
    "OpenClawRouter",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    env=env,
)

# Same-ID tombstone deployment removes any legacy Scheduler/Lambda/IAM
# resources from an existing OpenClawCron CloudFormation stack.
cron_stack = CronStack(app, "OpenClawCron", env=env)

# --- Observability (dashboards + alarms) ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- Token Monitoring ---
token_monitoring_stack = TokenMonitoringStack(
    app,
    "OpenClawTokenMonitoring",
    invocation_log_group=observability_stack.invocation_log_group,
    alarm_topic=observability_stack.alarm_topic,
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
