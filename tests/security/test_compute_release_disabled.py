"""Safe-release proof that incomplete production compute is inactive."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from release_tools.contracts import FOUNDATION_RELEASE_STACKS


ROOT = Path(__file__).resolve().parents[2]
REGION = "eu-west-1"
COMPUTE_ISOLATION_ALARM_NAME = "personal-operator-compute-isolation-failure"
FORBIDDEN_COMPUTE_IDENTIFIERS = (
    "personal-operator-compute",
    "ComputeInputBucket",
    "ComputeOutputBucket",
    "ComputeJobExecutionRole",
    "ComputeJobTaskDefinition",
    "ComputeWorkloadSecurityGroup",
    "personal-operator-compute-exec-",
    "personal-operator-compute-inputs",
    "personal-operator-compute-outputs",
    "/personal-operator/compute/job-runner",
    "/personal-operator-compute",
)
FORBIDDEN_COMPUTE_RESOURCE_TYPES = frozenset(
    {
        "AWS::Batch::ComputeEnvironment",
        "AWS::Batch::JobDefinition",
        "AWS::ECS::Cluster",
        "AWS::ECS::Service",
        "AWS::ECS::TaskDefinition",
    }
)


def _synth_active_application(tmp_path: Path) -> tuple[dict, list[dict]]:
    """Synth an exact source copy with a tiny deterministic web asset."""

    source = tmp_path / "source"
    source.mkdir()
    for filename in ("app.py", "cdk.json"):
        shutil.copy2(ROOT / filename, source / filename)
    for directory in ("lambda", "release_tools", "specs", "stacks"):
        shutil.copytree(
            ROOT / directory,
            source / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    web_dist = source / "web" / "dist"
    web_dist.mkdir(parents=True)
    shutil.copy2(ROOT / "tests/fixtures/web-dist/index.html", web_dist / "index.html")

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
        [sys.executable, str(source / "app.py")],
        cwd=source,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    artifacts = {
        artifact_id: artifact
        for artifact_id, artifact in manifest["artifacts"].items()
        if artifact.get("type") == "aws:cloudformation:stack"
    }
    templates = [
        json.loads(
            (outdir / artifact["properties"]["templateFile"]).read_text(
                encoding="utf-8"
            )
        )
        for artifact in artifacts.values()
    ]
    return artifacts, templates


def _assert_no_incomplete_compute_resources(
    resources: list[tuple[str, str, dict]],
) -> None:
    alarm_candidates = [
        entry
        for entry in resources
        if entry[2].get("Properties", {}).get("AlarmName")
        == COMPUTE_ISOLATION_ALARM_NAME
    ]
    assert len(alarm_candidates) == 1, (
        "expected one exact compute-isolation alarm, found "
        f"{len(alarm_candidates)}"
    )
    alarm_stack, alarm_logical_id, alarm = alarm_candidates[0]
    alarm_properties = alarm.get("Properties", {})
    alarm_actions = alarm_properties.get("AlarmActions")
    assert alarm_actions is not None and len(alarm_actions) == 1
    action_ref = alarm_actions[0].get("Ref")
    assert isinstance(action_ref, str) and action_ref
    expected_alarm_properties = {
        "AlarmActions": [{"Ref": action_ref}],
        "AlarmName": COMPUTE_ISOLATION_ALARM_NAME,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "Dimensions": [
            {"Name": "Component", "Value": "compute"},
            {"Name": "Environment", "Value": "preproduction"},
            {"Name": "Operation", "Value": "compute_isolation"},
            {"Name": "Outcome", "Value": "failed"},
        ],
        "EvaluationPeriods": 1,
        "MetricName": "EventCount",
        "Namespace": "PersonalOperator/Pilot",
        "Period": 300,
        "Statistic": "Sum",
        "Threshold": 1,
        "TreatMissingData": "notBreaching",
    }
    assert alarm == {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": expected_alarm_properties,
    }
    action_targets = [
        resource
        for stack_name, logical_id, resource in resources
        if stack_name == alarm_stack and logical_id == action_ref
    ]
    assert len(action_targets) == 1
    assert action_targets[0].get("Type") == "AWS::SNS::Topic"
    assert action_targets[0].get("Properties", {}).get("TopicName") == (
        "openclaw-alarms"
    )

    for stack_name, logical_id, resource in resources:
        if (stack_name, logical_id) == (alarm_stack, alarm_logical_id):
            continue
        assert resource.get("Type") not in FORBIDDEN_COMPUTE_RESOURCE_TYPES, (
            f"{logical_id} has forbidden compute resource type "
            f"{resource.get('Type')}"
        )
        rendered = json.dumps(
            {"logicalId": logical_id, "resource": resource},
            sort_keys=True,
            separators=(",", ":"),
        )
        for forbidden in FORBIDDEN_COMPUTE_IDENTIFIERS:
            assert forbidden not in rendered, (
                f"{logical_id} contains forbidden compute identifier {forbidden}"
            )


def test_compute_identifier_guard_rejects_non_alarm_resource_mutation() -> None:
    mutated_resources = [
        (
            "mutant-stack",
            "AlarmTopic",
            {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": "openclaw-alarms"},
            },
        ),
        (
            "mutant-stack",
            "ComputeIsolationFailureAlarm",
            {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {
                    "AlarmActions": [{"Ref": "AlarmTopic"}],
                    "AlarmName": COMPUTE_ISOLATION_ALARM_NAME,
                    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                    "Dimensions": [
                        {"Name": "Component", "Value": "compute"},
                        {"Name": "Environment", "Value": "preproduction"},
                        {"Name": "Operation", "Value": "compute_isolation"},
                        {"Name": "Outcome", "Value": "failed"},
                    ],
                    "EvaluationPeriods": 1,
                    "MetricName": "EventCount",
                    "Namespace": "PersonalOperator/Pilot",
                    "Period": 300,
                    "Statistic": "Sum",
                    "Threshold": 1,
                    "TreatMissingData": "notBreaching",
                },
            },
        ),
        (
            "mutant-stack",
            "NonAlarmComputeMutation",
            {
                "Type": "AWS::CloudWatch::Dashboard",
                "Properties": {"DashboardName": "personal-operator-compute"},
            },
        )
    ]

    with pytest.raises(AssertionError, match="NonAlarmComputeMutation"):
        _assert_no_incomplete_compute_resources(mutated_resources)


def test_active_synth_has_no_incomplete_compute_resources_or_authority(
    tmp_path: Path,
) -> None:
    artifacts, templates = _synth_active_application(tmp_path)
    resources = [
        (str(stack_index), logical_id, resource)
        for stack_index, template in enumerate(templates)
        for logical_id, resource in template.get("Resources", {}).items()
    ]

    assert "PersonalOperatorCompute" not in artifacts
    _assert_no_incomplete_compute_resources(resources)

    reports = sorted((tmp_path / "cdk.out").glob("AwsSolutions--*-NagReport.csv"))
    assert reports
    findings = []
    for report in reports:
        with report.open(newline="", encoding="utf-8") as handle:
            findings.extend(
                row
                for row in csv.DictReader(handle)
                if row["Compliance"] == "Non-Compliant"
            )
    assert findings == []


def test_active_composition_has_no_compute_launcher_or_context_wiring() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    composition_source = (
        ROOT / "lambda" / "capabilities" / "composition.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "ComputeStack",
        "compute_image_digest",
        "ProductionComputeRunner",
        "ComputeNetworkBinding",
        "ComputeLauncher",
        "JobStager",
    ):
        assert forbidden not in app_source
    assert "from compute" not in composition_source
    assert "import compute" not in composition_source


def test_active_release_contract_does_not_observe_or_deploy_compute() -> None:
    assert "PersonalOperatorCompute" not in FOUNDATION_RELEASE_STACKS


def test_compute_harness_and_product_docs_do_not_claim_isolation_or_completion() -> None:
    runner = (ROOT / "lambda" / "compute" / "runner.py").read_text(
        encoding="utf-8"
    )
    capability_boundary = (ROOT / "docs/CAPABILITY-BOUNDARY.md").read_text(
        encoding="utf-8"
    )
    privacy_boundary = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(
        encoding="utf-8"
    )
    task_report = (ROOT / ".superpowers/sdd/v1-task-8-report.md").read_text(
        encoding="utf-8"
    )
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")

    normalized_runner = " ".join(runner.split()).casefold()
    assert "defense in depth, not a security or isolation boundary" in normalized_runner
    for document in (capability_boundary, privacy_boundary, task_report):
        normalized = " ".join(document.split())
        assert "ADAPTER_DISABLED" in normalized
        assert "Task 8 operational completion remains OPEN" in normalized
    normalized_operations = " ".join(operations.split())
    assert "Production compute is not an active release stack" in normalized_operations
    assert "ADAPTER_DISABLED" in normalized_operations
    assert "Task 8 operational completion remains OPEN" in normalized_operations
