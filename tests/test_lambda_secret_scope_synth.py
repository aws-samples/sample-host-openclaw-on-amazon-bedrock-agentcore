"""Synth checks: the Router and Cron Lambda roles read only their own secrets.

Both functions get fixed *_SECRET_ID env vars for the channel tokens (and, for
the Router, the webhook secret). Their roles must allow exactly those secrets,
not the whole openclaw/ prefix that also holds the Cognito password secret, the
gateway token and every user's openclaw/user/<namespace>/ secrets.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.test_runtime_routing_synth import ACCOUNT, REGION, ROOT

SM_ACTIONS = {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
SUFFIX = "-??????"


def _synth(out, **extra):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
        "runtime_manifest": "",
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
        **extra,
    })
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({"CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(out),
                "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true"})
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return out


def _resolve(value):
    """Render a Fn::Join of plain strings / pseudo refs to one string."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_resolve(part) for part in parts)
    if isinstance(value, dict) and "Ref" in value:
        return "<" + value["Ref"] + ">"
    return json.dumps(value, sort_keys=True)


def _function_env(resources, env_key):
    (fn,) = [value for value in resources.values() if value["Type"] == "AWS::Lambda::Function"
             and env_key in value["Properties"].get("Environment", {}).get("Variables", {})
             and "TELEGRAM_TOKEN_SECRET_ID" in value["Properties"]["Environment"]["Variables"]]
    return fn


def _role_secret_resources(resources, fn):
    role_ref = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    found = []
    for value in resources.values():
        if value["Type"] != "AWS::IAM::Policy" or {"Ref": role_ref} not in value["Properties"]["Roles"]:
            continue
        for statement in value["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            if any(action.startswith("secretsmanager:") for action in actions):
                assert set(actions) <= SM_ACTIONS, actions
                listed = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
                found.extend(_resolve(item) for item in listed)
    return found


def _expected(fn):
    variables = fn["Properties"]["Environment"]["Variables"]
    names = [_resolve(value) for key, value in variables.items() if key.endswith("_SECRET_ID") and value]
    return sorted(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}{SUFFIX}" for name in names)


@pytest.fixture(scope="module", params=["", "stg"], ids=["default", "suffixed"])
def stacks(request, tmp_path_factory):
    out = _synth(tmp_path_factory.mktemp("secret-scope") / "cdk.out", environment_suffix=request.param)
    suffix = f"-{request.param}" if request.param else ""
    load = lambda name: json.loads((out / f"{name}{suffix}.template.json").read_text())["Resources"]
    return load("OpenClawRouter"), load("OpenClawCron")


@pytest.mark.parametrize("which, env_key", [(0, "WEBHOOK_SECRET_ID"), (1, "LINE_TOKEN_SECRET_ID")],
                         ids=["router", "cron"])
def test_lambda_reads_exactly_its_configured_secrets(stacks, which, env_key):
    resources = stacks[which]
    fn = _function_env(resources, env_key)
    granted = sorted(_role_secret_resources(resources, fn))
    assert granted == _expected(fn)
    for arn in granted:
        assert "*" not in arn
        assert "cognito-password-secret" not in arn and "gateway-token" not in arn and "/user/" not in arn


def test_cron_keeps_listing_for_marker_less_brief_audio(stacks):
    resources = stacks[1]
    fn = _function_env(resources, "LINE_TOKEN_SECRET_ID")
    role_ref = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    actions = {action for value in resources.values() if value["Type"] == "AWS::IAM::Policy"
               and {"Ref": role_ref} in value["Properties"]["Roles"]
               for statement in value["Properties"]["PolicyDocument"]["Statement"]
               for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])}
    assert "s3:ListBucket" in actions
