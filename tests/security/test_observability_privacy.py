from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from stacks.observability_stack import ObservabilityStack
from tests.test_product_configuration import _synth_router_template
from tests.test_web_stack import _synth_web_template


ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CMK_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key"


def _rendered(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def test_all_api_access_logs_have_one_exact_closed_metadata_schema() -> None:
    expected = {
        "latency": "$context.responseLatency",
        "method": "$context.httpMethod",
        "responseLength": "$context.responseLength",
        "route": "$context.routeKey",
        "status": "$context.status",
    }
    observed = []
    for template in (_synth_router_template(), _synth_web_template()):
        stages = _resources(template, "AWS::ApiGatewayV2::Stage")
        assert len(stages) == 1
        settings = stages[0]["Properties"]["AccessLogSettings"]
        assert settings["DestinationArn"]
        observed.append(json.loads(settings["Format"]))

    assert observed == [expected, expected]


def test_observability_template_keeps_metrics_without_model_payload_logging() -> None:
    app = App()
    stack = ObservabilityStack(
        app,
        "Observability",
        cmk_arn=CMK_ARN,
        env=Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    rendered = _rendered(template)

    assert "AWS::CloudWatch::Dashboard" in rendered
    assert "AWS::CloudWatch::Alarm" in rendered
    assert "AWS::SNS::Topic" in rendered
    assert "PutModelInvocationLoggingConfiguration" not in rendered
    assert "GetModelInvocationLoggingConfiguration" not in rendered
    assert "textDataDeliveryEnabled" not in rendered
    assert "imageDataDeliveryEnabled" not in rendered
    assert "/aws/bedrock/invocation-logs" not in rendered

    dashboard = _resources(template, "AWS::CloudWatch::Dashboard")[0]
    separator, fragments = dashboard["Properties"]["DashboardBody"]["Fn::Join"]
    body = separator.join(
        fragment if isinstance(fragment, str) else "REGION" for fragment in fragments
    )
    expected_agentcore_searches = {
        "SEARCH('{AWS/Bedrock-AgentCore} "
        f'MetricName="{metric_name}"\', \'{statistic}\', 300)'
        for metric_name, statistic in {
            "Invocations": "Sum",
            "SystemErrors": "Sum",
            "UserErrors": "Sum",
            "Throttles": "Sum",
            "Latency": "p99",
        }.items()
    }
    dashboard_body = json.loads(body)
    agentcore_searches = {
        metric[0]["expression"]
        for widget in dashboard_body["widgets"]
        for metric in widget.get("properties", {}).get("metrics", [])
        if isinstance(metric[0], dict)
        and metric[0].get("label", "").startswith("AgentCore ")
    }
    assert agentcore_searches == expected_agentcore_searches
    assert "AWS/BedrockAgentCore" not in body
    assert "InvocationErrors" not in body
    assert "InvocationLatency" not in body.split(
        'title":"AgentCore Runtime Latency (p99)"', maxsplit=1
    )[1]


def test_private_pilot_alarm_set_is_complete_without_duplicate_dlq_or_publishers() -> None:
    app = App()
    stack = ObservabilityStack(
        app,
        "ObservabilityAlarms",
        cmk_arn=CMK_ARN,
        env=Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    alarms = {
        alarm["Properties"]["AlarmName"]: alarm["Properties"]
        for alarm in _resources(template, "AWS::CloudWatch::Alarm")
    }

    expected_custom = {
        "personal-operator-uncertain-effect": (
            "action_kernel",
            "uncertain_effect",
            "uncertain",
            1,
        ),
        "personal-operator-repeated-scan-failure": (
            "scan",
            "scan",
            "failed",
            3,
        ),
        "personal-operator-aged-deletion": (
            "portable",
            "deletion",
            "aged",
            1,
        ),
        "personal-operator-connector-drift": (
            "connector",
            "connector_drift",
            "drifted",
            1,
        ),
        "personal-operator-compute-isolation-failure": (
            "compute",
            "compute_isolation",
            "failed",
            1,
        ),
    }
    assert set(expected_custom).issubset(alarms)
    assert "personal-operator-missing-maintenance-heartbeat" in alarms
    assert "personal-operator-telegram-dlq-visible" not in alarms

    for name, (component, operation, outcome, threshold) in expected_custom.items():
        properties = alarms[name]
        assert properties["Namespace"] == "PersonalOperator/Pilot"
        assert properties["MetricName"] == "EventCount"
        assert properties["Threshold"] == threshold
        assert properties["TreatMissingData"] == "notBreaching"
        assert properties["Dimensions"] == [
            {"Name": "Component", "Value": component},
            {"Name": "Environment", "Value": "preproduction"},
            {"Name": "Operation", "Value": operation},
            {"Name": "Outcome", "Value": outcome},
        ]

    heartbeat = alarms["personal-operator-missing-maintenance-heartbeat"]
    assert heartbeat["Namespace"] == "AWS/Lambda"
    assert heartbeat["MetricName"] == "Invocations"
    assert heartbeat["Dimensions"] == [
        {"Name": "FunctionName", "Value": "personal-operator-maintenance"}
    ]
    assert heartbeat["TreatMissingData"] == "breaching"

    resource_types = {resource["Type"] for resource in template["Resources"].values()}
    assert "AWS::Lambda::Function" not in resource_types
    assert "AWS::IAM::Role" not in resource_types
    assert "AWS::IAM::Policy" not in resource_types
    assert "AWS::Logs::MetricFilter" not in resource_types


def test_existing_router_dlq_alarm_remains_the_single_exact_live_alarm() -> None:
    router = _synth_router_template()
    matches = [
        alarm["Properties"]
        for alarm in _resources(router, "AWS::CloudWatch::Alarm")
        if alarm["Properties"].get("AlarmName")
        == "personal-operator-telegram-dlq-visible"
    ]

    assert len(matches) == 1
    assert matches[0] == {
        "AlarmName": "personal-operator-telegram-dlq-visible",
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "Dimensions": [
            {
                "Name": "QueueName",
                "Value": {
                    "Fn::GetAtt": ["TelegramDeadLetterQueue94187138", "QueueName"]
                },
            }
        ],
        "EvaluationPeriods": 1,
        "MetricName": "ApproximateNumberOfMessagesVisible",
        "Namespace": "AWS/SQS",
        "Period": 60,
        "Statistic": "Maximum",
        "Threshold": 1,
        "TreatMissingData": "notBreaching",
    }


def test_active_app_synth_has_no_payload_logging_or_legacy_token_monitoring(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "cdk.out"
    env = os.environ.copy()
    for name in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        env.pop(name, None)
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "CDK_DEFAULT_ACCOUNT": "000000000000",
            "CDK_DEFAULT_REGION": REGION,
            "CDK_OUTDIR": str(outdir),
            "PERSONAL_OPERATOR_SYNTH_SOURCE_ASSET": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(ROOT / "app.py")],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    stack_artifacts = {
        artifact_id: artifact
        for artifact_id, artifact in manifest["artifacts"].items()
        if artifact.get("type") == "aws:cloudformation:stack"
    }
    assert "OpenClawObservability" in stack_artifacts
    assert "OpenClawTokenMonitoring" not in stack_artifacts

    templates = []
    for artifact in stack_artifacts.values():
        template_file = artifact["properties"]["templateFile"]
        templates.append(
            json.loads((outdir / template_file).read_text(encoding="utf-8"))
        )
    rendered = _rendered(templates)
    assert "PutModelInvocationLoggingConfiguration" not in rendered
    assert "textDataDeliveryEnabled" not in rendered
    assert "imageDataDeliveryEnabled" not in rendered
    assert "openclaw-token-metrics" not in rendered
    assert "OpenClaw/TokenUsage" not in rendered
    assert "openclaw-token-usage" not in rendered

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    deploy_source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    for source in (app_source, deploy_source):
        assert "OpenClawTokenMonitoring" not in source
    assert "TokenMonitoringStack" not in app_source


def test_retained_legacy_token_stack_is_clearly_archived() -> None:
    legacy_stack_source = (ROOT / "stacks/token_monitoring_stack.py").read_text(
        encoding="utf-8"
    )

    assert "Archived upstream reference" in legacy_stack_source
    assert "must not be instantiated" in legacy_stack_source


def test_current_boundary_docs_disclose_metadata_only_observability() -> None:
    privacy = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for document in (privacy, operations, readme):
        normalized = " ".join(document.split())
        assert "model invocation text or image payload logging" in normalized
        assert "DISABLE_ADOT_OBSERVABILITY=true" in normalized
        assert "payload-rich AgentCore application observability" in normalized
        assert "legacy token-monitoring stack is not active" in normalized

    normalized_privacy = " ".join(privacy.split())
    assert "ordinary platform operational logs remain" in normalized_privacy
    assert "live CloudWatch inspection remains OPEN" in normalized_privacy


def test_privacy_boundary_retires_logs_as_an_application_data_transport() -> None:
    privacy = " ".join(
        (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8").split()
    )

    assert (
        "Application-emitted runtime and router message fields contain only closed "
        "metadata" in privacy
    )
    assert "platform envelopes/system records still add operational" in privacy
    assert "exact live retained-field inspection remains OPEN" in privacy
    assert "CloudWatch is not a response-inspection transport" in privacy
    assert "Safe operational fields are bounded internal user IDs" not in privacy
