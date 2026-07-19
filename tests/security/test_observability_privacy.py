from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from stacks.observability_stack import ObservabilityStack


ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CMK_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key"


def _rendered(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
        assert "legacy token-monitoring stack is not active" in normalized


def test_privacy_boundary_retires_logs_as_an_application_data_transport() -> None:
    privacy = " ".join(
        (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8").split()
    )

    assert "retained runtime and router logs contain only closed metadata" in privacy
    assert "CloudWatch is not a response-inspection transport" in privacy
    assert "Safe operational fields are bounded internal user IDs" not in privacy
