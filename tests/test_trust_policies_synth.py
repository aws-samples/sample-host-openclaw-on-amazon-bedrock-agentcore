"""Synth-time checks that service principals are scoped to this account.

Runs app.py offline (no AWS calls, no context lookups) and inspects the
CMK key policy and the role trust policies.

    pytest tests/test_trust_policies_synth.py -v
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACCOUNT, REGION = "123456789012", "us-west-2"


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    out = tmp_path_factory.mktemp("cdk.out")
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
    })
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE",
                                            "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({
        "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(out),
        "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION,
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not json.loads((out / "manifest.json").read_text()).get("missing")
    return {p.name.split(".")[0]: json.loads(p.read_text()) for p in out.glob("*.template.json")}


def _resources(template, rtype):
    return [r for r in template["Resources"].values() if r["Type"] == rtype]


def _role_named(template, suffix):
    for role in _resources(template, "AWS::IAM::Role"):
        if json.dumps(role["Properties"].get("RoleName", "")).find(suffix) >= 0:
            return role
    raise AssertionError(f"no role named *{suffix}*")


def test_cloudwatch_key_grant_is_limited_to_this_accounts_alarms(templates):
    keys = _resources(templates["OpenClawSecurity"], "AWS::KMS::Key")
    stmts = [s for k in keys for s in k["Properties"]["KeyPolicy"]["Statement"]
             if s.get("Principal", {}).get("Service") == "cloudwatch.amazonaws.com"]
    assert len(stmts) == 1
    cond = stmts[0]["Condition"]
    assert cond["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    assert cond["ArnLike"] == {"aws:SourceArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:*"}


def test_execution_role_trusts_only_agentcore_in_this_account(templates):
    role = _role_named(templates["OpenClawAgentCore"], "openclaw-agentcore-execution-role")
    stmts = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    services = [s["Principal"]["Service"] for s in stmts if "Service" in s.get("Principal", {})]
    assert services == ["bedrock-agentcore.amazonaws.com"]
    service_stmt = next(s for s in stmts if "Service" in s.get("Principal", {}))
    assert service_stmt["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    # Account-and-Region wildcard (not runtime/*): the same role backs the custom Browser.
    assert service_stmt["Condition"]["ArnLike"] == {
        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}
    # Per-user scoped-credential self-assume is unchanged.
    self_assume = [s for s in stmts if "AWS" in s.get("Principal", {})]
    assert len(self_assume) == 1
    assert set(self_assume[0]["Condition"]) == {"ArnEquals", "StringLike"}
    assert self_assume[0]["Condition"]["StringLike"] == {"sts:RoleSessionName": "scoped-*"}


def test_scheduler_role_trusts_only_this_accounts_schedules(templates):
    role = _role_named(templates["OpenClawCron"], "openclaw-cron-scheduler-role")
    (stmt,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
