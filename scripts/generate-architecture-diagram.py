#!/usr/bin/env python3
"""Generate AWS architecture diagram for OpenClaw on AgentCore.

Requires: pip install diagrams, apt/yum install graphviz
Usage: python3 scripts/generate-architecture-diagram.py
Output: docs/openclaw-agentcore-architecture.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Fargate, Lambda, ElasticContainerServiceService
from diagrams.aws.network import CloudFront, ElbApplicationLoadBalancer, VPC, Endpoint
from diagrams.aws.database import Dynamodb
from diagrams.aws.security import WAF, Cognito, SecretsManager, KMS, IdentityAndAccessManagementIam
from diagrams.aws.management import Cloudwatch, CloudwatchAlarm, Cloudtrail
from diagrams.aws.integration import SimpleNotificationServiceSns
from diagrams.aws.ml import Bedrock
from diagrams.aws.general import Client, MobileClient
from diagrams.custom import Custom
import os

# Output config
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "openclaw-agentcore-architecture")

graph_attr = {
    "fontsize": "14",
    "bgcolor": "white",
    "pad": "0.5",
    "nodesep": "0.8",
    "ranksep": "1.0",
}

with Diagram(
    "OpenClaw on AgentCore",
    filename=OUTPUT_PATH,
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    outformat="png",
):
    # ── Users ──
    telegram = MobileClient("Telegram")
    discord = MobileClient("Discord")
    slack = MobileClient("Slack")
    browser = Client("Web Browser")

    # ── Edge Layer ──
    with Cluster("Edge Layer (OpenClawEdge)"):
        cf = CloudFront("CloudFront\nd34s8ria53v6u2")
        waf = WAF("WAF\n100 req/5min")

    # ── Security & Identity ──
    with Cluster("Security & Identity (OpenClawSecurity)"):
        cognito = Cognito("Cognito User Pool\nadmin-provisioned\nHMAC passwords")
        secrets = SecretsManager("Secrets Manager\ngateway + channel\n+ HMAC secret")
        kms = KMS("KMS CMK")
        trail = Cloudtrail("CloudTrail")

    # ── VPC ──
    with Cluster("VPC 10.0.0.0/16 — ap-southeast-2 (OpenClawVpc)"):

        with Cluster("Public Subnets"):
            alb = ElbApplicationLoadBalancer("Public ALB\nCF IPs only")

        with Cluster("Private Subnets"):
            vpce = Endpoint("VPC Endpoints\nBedrock, ECR, SSM\nSecrets Mgr, CW")

            with Cluster("ECS Fargate (OpenClawFargate)"):
                fargate = Fargate("Fargate Task\n256 CPU / 1024 MiB")
                with Cluster("OpenClaw Gateway\nport 18789 — WebSocket + Web UI"):
                    openclaw = ElasticContainerServiceService("OpenClaw\nChannel Providers")
                with Cluster("agentcore-proxy.js\nport 18790 — OpenAI→Bedrock"):
                    proxy = ElasticContainerServiceService("Proxy Adapter\nCognito + JWT + SSE")

            # ── Bedrock Direct ──
            with Cluster("Bedrock Direct (bedrock-direct mode)"):
                bedrock_direct = Bedrock("Bedrock API\nConverseStream\nClaude Sonnet 4.6")

            # ── AgentCore Runtime ──
            with Cluster("AgentCore Runtime (agentcore mode)\nOpenClawAgentCore"):
                runtime = Bedrock("CfnRuntime\nopenclaw_agent\nStrands Agent")
                rt_endpoint = Endpoint("RuntimeEndpoint\nopenclaw_agent_live")
                memory = Bedrock("AgentCore Memory\nsemantic + user-prefs\n+ summary (90d)")
                bedrock_agent = Bedrock("Bedrock\nClaude Sonnet 4.6")
                workload_id = IdentityAndAccessManagementIam("WorkloadIdentity\nopenclaw_identity\nJWT Authorizer")

    # ── Observability ──
    with Cluster("Observability & Token Monitoring\n(OpenClawObservability + OpenClawTokenMonitoring)"):
        cw_logs = Cloudwatch("CloudWatch Logs\nInvocation Logs")
        lam = Lambda("Lambda\nToken Processor")
        ddb = Dynamodb("DynamoDB\nToken Usage\nSingle-table")
        cw_dash = CloudwatchAlarm("Dashboards\n+ Budget Alarms")
        sns = SimpleNotificationServiceSns("SNS\nAlarm Topic")

    # ═══════════ CONNECTIONS ═══════════

    # Users → Edge
    telegram >> Edge(color="gray") >> cf
    discord >> Edge(color="gray") >> cf
    slack >> Edge(color="gray") >> cf
    browser >> Edge(color="gray") >> cf
    cf >> waf

    # Edge → ALB → Fargate
    waf >> Edge(label="HTTPS", color="blue") >> alb
    alb >> fargate >> openclaw >> proxy

    # Proxy → Bedrock Direct (default path)
    proxy >> Edge(label="bedrock-direct\n(default)", color="orange", style="bold") >> bedrock_direct

    # Proxy → AgentCore (feature-flagged path)
    proxy >> Edge(label="agentcore", color="purple", style="dashed") >> rt_endpoint
    rt_endpoint >> runtime
    runtime >> memory
    runtime >> bedrock_agent

    # Identity flow
    proxy >> Edge(label="JWT", color="red", style="dashed") >> cognito
    cognito >> Edge(label="OIDC", color="red", style="dashed") >> workload_id

    # Secrets
    secrets >> Edge(color="red", style="dotted") >> proxy

    # Observability pipeline
    bedrock_direct >> Edge(label="invocation\nlogs", color="pink", style="dashed") >> cw_logs
    cw_logs >> lam >> ddb >> cw_dash >> sns


print(f"Diagram generated: {OUTPUT_PATH}.png")
