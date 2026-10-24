"""Synth-time tests for the audit trail and log group removal policies.

The dedicated CloudTrail stays opt-in (``enable_cloudtrail``). When enabled it
is multi-region and records S3 data events on the user-files bucket. The
named ``/openclaw/*`` and Bedrock invocation log groups are retained on stack
delete, like the API access and VPC flow log groups.

The whole ``app.py`` is synthesized in a child process (see
``test_table_protection_synth.py``). No AWS calls.

    pytest tests/test_audit_logging_synth.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"
_REGION = "us-west-2"


def _synth_app(tmp_path, **context_overrides):
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    context.update(
        {
            "account": _ACCOUNT,
            "region": _REGION,
            "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
        }
    )
    context.update(context_overrides)
    outdir = tmp_path / "cdk.out"
    env = dict(os.environ, CDK_CONTEXT_JSON=json.dumps(context), CDK_OUTDIR=str(outdir))
    result = subprocess.run(
        [sys.executable, "app.py"],
        cwd=_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return {
        p.name.removesuffix(".template.json"): json.loads(p.read_text())
        for p in outdir.glob("*.template.json")
    }


def _resources(templates, cfn_type):
    return {
        f"{stack}/{lid}": res
        for stack, t in templates.items()
        for lid, res in t.get("Resources", {}).items()
        if res["Type"] == cfn_type
    }


@pytest.fixture(scope="module")
def default_templates(tmp_path_factory):
    return _synth_app(tmp_path_factory.mktemp("default"))


@pytest.fixture(scope="module")
def trail_templates(tmp_path_factory):
    return _synth_app(tmp_path_factory.mktemp("trail"), enable_cloudtrail=True)


def test_cloudtrail_is_opt_in():
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    assert context["enable_cloudtrail"] is False


def test_no_trail_by_default(default_templates):
    assert _resources(default_templates, "AWS::CloudTrail::Trail") == {}


def test_trail_is_multi_region_with_user_files_data_events(trail_templates):
    trails = _resources(trail_templates, "AWS::CloudTrail::Trail")
    assert len(trails) == 1
    props = next(iter(trails.values()))["Properties"]
    assert props["IsMultiRegionTrail"] is True
    selectors = props["EventSelectors"]
    assert selectors[0]["ReadWriteType"] == "All"
    values = json.dumps(selectors[0]["DataResources"])
    assert "AWS::S3::Object" in values
    assert f"openclaw-user-files-{_ACCOUNT}-{_REGION}/" in values


def test_trail_log_group_is_retained(trail_templates):
    groups = {
        k: v for k, v in _resources(trail_templates, "AWS::Logs::LogGroup").items()
        if k.startswith("OpenClawSecurity/")
    }
    assert groups
    for key, res in groups.items():
        assert res.get("DeletionPolicy") == "Retain", key


@pytest.mark.parametrize("name", [
    "/openclaw/lambda/router",
    "/openclaw/lambda/cron",
    "/openclaw/states/cron-runner",
    "/openclaw/lambda/token-metrics",
    "/aws/bedrock/invocation-logs",
])
def test_named_log_groups_are_retained(default_templates, name):
    matches = [
        (k, r) for k, r in _resources(default_templates, "AWS::Logs::LogGroup").items()
        if r["Properties"].get("LogGroupName") == name
    ]
    assert len(matches) == 1, name
    key, res = matches[0]
    assert res.get("DeletionPolicy") == "Retain", key
    assert res.get("UpdateReplacePolicy") == "Retain", key
