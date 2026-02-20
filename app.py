#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: OpenClaw runs directly on AgentCore Runtime (serverless),
replacing ECS Fargate. A keepalive Lambda ensures the session stays active.
"""

import os

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.keepalive_stack import KeepaliveStack
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

# --- AgentCore (hosts OpenClaw container directly) ---
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

# --- Keepalive (Lambda + EventBridge to keep the AgentCore session alive) ---
keepalive_stack = KeepaliveStack(
    app,
    "OpenClawKeepalive",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    env=env,
)

# --- Observability (dashboards + alarms — adapted for AgentCore) ---
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
