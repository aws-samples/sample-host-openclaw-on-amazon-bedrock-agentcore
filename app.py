#!/usr/bin/env python3
"""OpenClaw on AgentCore — CDK Application entry point."""

import aws_cdk as cdk
import cdk_nag
import boto3

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.fargate_stack import FargateStack
from stacks.edge_stack import EdgeStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region"),
)

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- AgentCore ---
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    cognito_issuer_url=security_stack.cognito_issuer_url,
    cognito_client_id=security_stack.user_pool_client_id,
    env=env,
)


# --- Fargate ---
fargate_stack = FargateStack(
    app,
    "OpenClawFargate",
    vpc=vpc_stack.vpc,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    runtime_id=agentcore_stack.runtime_id,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    memory_id=agentcore_stack.memory_id,
    cognito_user_pool_id=security_stack.user_pool_id,
    cognito_client_id=security_stack.user_pool_client_id,
    cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
    env=env,
)
# Dependencies are inferred via cross-stack references (vpc, fargate_sg, secrets, cmk)

# --- Read gateway token for CloudFront Function validation ---
_gateway_token = ""
try:
    _sm = boto3.client(
        "secretsmanager",
        region_name=app.node.try_get_context("region") or "ap-southeast-2",
    )
    _gateway_token = _sm.get_secret_value(SecretId="openclaw/gateway-token")[
        "SecretString"
    ]
except Exception:
    pass  # Token unavailable (first deploy or no creds) — falls back to presence-only check

# --- Edge (CloudFront + WAF) ---
edge_stack = EdgeStack(
    app,
    "OpenClawEdge",
    alb=fargate_stack.public_alb,
    gateway_token=_gateway_token,
    env=env,
)


# --- Observability ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    fargate_service=fargate_stack.service,
    cluster_name=fargate_stack.cluster.cluster_name,
    service_name=fargate_stack.service.service_name,
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
