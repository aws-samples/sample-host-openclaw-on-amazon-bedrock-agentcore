#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: Per-user AgentCore Runtime sessions with webhook-based
channel ingestion via Router Lambda. No keepalive needed — sessions
idle-terminate naturally.
"""

import os

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
)

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- AgentCore (hosts OpenClaw container, per-user sessions) ---
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    cognito_issuer_url=security_stack.cognito_issuer_url,
    cognito_client_id=security_stack.user_pool_client_id,
    cognito_user_pool_id=security_stack.user_pool_id,
    cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    env=env,
)

# --- Router (Lambda + API Gateway HTTP API for Telegram/Slack webhooks) ---
router_stack = RouterStack(
    app,
    "OpenClawRouter",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    env=env,
)

# --- Observability (dashboards + alarms) ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    env=env,
)

# --- Token Monitoring ---
token_monitoring_stack = TokenMonitoringStack(
    app,
    "OpenClawTokenMonitoring",
    invocation_log_group=observability_stack.invocation_log_group,
    alarm_topic=observability_stack.alarm_topic,
    env=env,
)

# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
