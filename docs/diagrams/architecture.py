"""README architecture diagram (AWS icons, high level).

Regenerate: python3 -m venv .venv && .venv/bin/pip install diagrams && .venv/bin/python docs/diagrams/architecture.py
(requires Graphviz `dot`; writes docs/images/architecture.png)

Detailed internals (contract server, proxy, lightweight agent, KMS, VPC
endpoints, ECR) are intentionally left out — see docs/architecture-detailed.md.
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import EventbridgeScheduler
from diagrams.aws.management import Cloudwatch
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway
from diagrams.aws.security import Cognito, SecretsManager
from diagrams.aws.storage import S3
from diagrams.onprem.client import Client, Users
from diagrams.saas.chat import Slack, Telegram

OUT = Path(__file__).resolve().parents[1] / "images" / "architecture"

GRAPH_ATTR = {
    "bgcolor": "white",
    "pad": "0.4",
    "nodesep": "0.55",
    "ranksep": "1.0",
    "fontsize": "16",
    "fontname": "Sans-Serif",
    "splines": "spline",
    "dpi": "110",
}
NODE_ATTR = {"fontsize": "13", "fontname": "Sans-Serif"}
EDGE_ATTR = {"fontsize": "11", "fontname": "Sans-Serif", "color": "#555555"}


def main(label=""):
    """Heavily weighted edge so the request path stays on one straight line."""
    return Edge(label=label, weight="10", penwidth="1.6", color="#232F3E")


def side(label="", **kw):
    """Light dashed edge for supporting services."""
    return Edge(label=label, style="dashed", **kw)


def same_rank(cluster, *nodes):
    """Pin nodes to one column inside a cluster (must be called inside its `with`)."""
    ids = "; ".join(f'"{n.nodeid}"' for n in nodes)
    cluster.dot.body.append(f"{{rank=same; {ids}}}")


with Diagram(
    "",
    filename=str(OUT),
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH_ATTR,
    node_attr=NODE_ATTR,
    edge_attr=EDGE_ATTR,
):
    users = Users("Users")

    with Cluster("Chat channels"):
        telegram = Telegram("Telegram")
        slack = Slack("Slack")
        feishu = Client("Feishu")
        channels = [telegram, slack, feishu]

    with Cluster("Ingress") as ingress:
        apigw = APIGateway("API Gateway\nHTTP API")
        router = Lambda("Router Lambda")
        identity_db = Dynamodb("DynamoDB\nusers, sessions")
        same_rank(ingress, router, identity_db)

    with Cluster("Scheduled tasks"):
        scheduler = EventbridgeScheduler("EventBridge\nScheduler")
        cron = Lambda("Cron Lambda")

    with Cluster("Per-user microVM"):
        runtime = Bedrock("OpenClaw 2.0 on\nAgentCore Runtime")

    with Cluster("Identity and secrets"):
        cognito = Cognito("Cognito + STS\nscoped credentials")
        secrets = SecretsManager("Secrets Manager\ntokens, API keys")

    bedrock = Bedrock("Amazon Bedrock\nClaude, Guardrails")
    s3 = S3("S3 workspace\nand user files")
    cloudwatch = Cloudwatch("CloudWatch\ntoken monitoring")

    # Main request path (left to right)
    users >> channels
    for ch in channels:
        ch >> main() >> apigw
    apigw >> main() >> router
    router >> main("InvokeAgentRuntime") >> runtime
    runtime >> main("ConverseStream") >> bedrock

    # Identity lookup (same column as the Router)
    router - side() - identity_db

    # Runtime side-effects (declared top-to-bottom around Bedrock)
    runtime >> side() >> cognito
    runtime >> side() >> secrets
    runtime - side() - s3

    # Scheduled tasks: the runtime creates schedules; the Cron Lambda fires them back
    runtime >> side("create schedule", constraint="false") >> scheduler
    scheduler >> cron
    cron >> Edge(label="cron action") >> runtime

    # Token monitoring
    bedrock >> Edge(label="invocation logs", style="dotted") >> cloudwatch
