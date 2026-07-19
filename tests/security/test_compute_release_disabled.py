"""Safe-release proof that incomplete production compute is inactive."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from release_tools.contracts import FOUNDATION_RELEASE_STACKS


ROOT = Path(__file__).resolve().parents[2]
REGION = "eu-west-1"


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


def test_active_synth_has_no_incomplete_compute_resources_or_authority(
    tmp_path: Path,
) -> None:
    artifacts, templates = _synth_active_application(tmp_path)
    resources = [
        resource
        for template in templates
        for resource in template.get("Resources", {}).values()
    ]
    rendered = json.dumps(templates, sort_keys=True, separators=(",", ":"))

    assert "PersonalOperatorCompute" not in artifacts
    assert not any(
        resource["Type"] in {"AWS::ECS::Cluster", "AWS::ECS::TaskDefinition"}
        for resource in resources
    )
    for forbidden in (
        "personal-operator-compute",
        "ComputeInputBucket",
        "ComputeOutputBucket",
        "ComputeJobExecutionRole",
        "ComputeJobTaskDefinition",
        "ComputeWorkloadSecurityGroup",
    ):
        assert forbidden not in rendered

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
