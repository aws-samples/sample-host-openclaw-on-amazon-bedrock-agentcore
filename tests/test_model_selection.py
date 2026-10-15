"""Per-user /model allowlist (issue #18): validation, synth wiring, IAM and runtime config.

Offline: synth uses fixed account/region/AZ context and fails if any context
lookup is requested; no AWS API is called.

    pytest tests/test_model_selection.py -v
"""

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.model_selection import allowlisted_model_ids, main, parse_user_selectable_models

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # runtime_config imports its sibling modules directly
from runtime_config import foundation_configuration  # noqa: E402
ACCOUNT, REGION = "123456789012", "us-west-2"
SHIPPED = {
    "haiku": "global.anthropic.claude-haiku-5-5",
    "sonnet": "global.anthropic.claude-sonnet-5-5",
    "opus": "global.anthropic.claude-opus-5-5",
}


def test_shipped_cdk_json_offers_the_latest_global_claude_models():
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    assert parse_user_selectable_models(context["user_selectable_models"]) == SHIPPED
    assert context["default_model_id"] in SHIPPED.values()


@pytest.mark.parametrize("disabled", [None, "", {}, "{}"])
def test_empty_or_missing_map_disables_the_feature(disabled):
    assert parse_user_selectable_models(disabled) == {}
    assert allowlisted_model_ids({}) == ""


def test_json_string_context_and_distinct_ids():
    models = parse_user_selectable_models(json.dumps({"fast": "a.b-1", "quick": "a.b-1", "big": "c.d:0"}))
    assert allowlisted_model_ids(models) == "a.b-1,c.d:0"


@pytest.mark.parametrize("value", [
    ["haiku"], "not json", 5,
    {"default": "global.anthropic.claude-haiku-5-5"},
    {"Opus": "global.anthropic.claude-opus-5-5"},
    {"my model": "global.anthropic.claude-opus-5-5"},
    {"x" * 33: "global.anthropic.claude-opus-5-5"},
    {"opus": ""}, {"opus": None}, {"opus": 5},
    {"opus": "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.anthropic.claude-opus-5-5"},
    {"opus": "global.anthropic.claude-opus-5-5,global.anthropic.claude-fable-5-1"},
    {"opus": "Global.Anthropic.Claude"},
    {f"m{i}": "global.anthropic.claude-haiku-5-5" for i in range(11)},
])
def test_invalid_maps_are_rejected(value):
    with pytest.raises(ValueError):
        parse_user_selectable_models(value)


def test_cli_prints_ids_and_fails_on_invalid(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"context": {"user_selectable_models": SHIPPED}}))
    assert main(["", str(good)]) == 0
    assert capsys.readouterr().out.strip() == ",".join(SHIPPED.values())
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"context": {}}))
    assert main(["", str(missing)]) == 0
    assert capsys.readouterr().out.strip() == ""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"context": {"user_selectable_models": {"default": "x.y"}}}))
    assert main(["", str(bad)]) == 1
    assert "reserved" in capsys.readouterr().err


FOUNDATION = {
    "EXECUTION_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/runtime", "SECURITY_GROUP_ID": "sg-1",
    "PRIVATE_SUBNET_IDS": "subnet-1", "USER_FILES_BUCKET": "bucket", "COGNITO_USER_POOL_ID": "pool",
    "COGNITO_CLIENT_ID": "client", "CMK_ARN": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/k", "REGION": REGION,
    "ACCOUNT": ACCOUNT, "SESSION_IDLE": "1800", "SESSION_MAX": "28800",
}


def test_runtime_configuration_sets_or_removes_the_allowlist():
    ids = ",".join(SHIPPED.values())
    configured = foundation_configuration({**FOUNDATION, "USER_SELECTABLE_MODEL_IDS": ids})
    assert configured["environmentVariables"]["BEDROCK_USER_SELECTABLE_MODEL_IDS"] == ids
    disabled = foundation_configuration({**FOUNDATION, "USER_SELECTABLE_MODEL_IDS": ""})
    assert "BEDROCK_USER_SELECTABLE_MODEL_IDS" not in disabled["environmentVariables"]
    assert "BEDROCK_USER_SELECTABLE_MODEL_IDS" in disabled["unsetEnvironmentVariables"]
    inherited = foundation_configuration(dict(FOUNDATION))
    assert "BEDROCK_USER_SELECTABLE_MODEL_IDS" not in inherited["environmentVariables"]
    assert "BEDROCK_USER_SELECTABLE_MODEL_IDS" not in inherited.get("unsetEnvironmentVariables", [])


def synth(tmp_path, **overrides):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
        "runtime_manifest": "",
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
        **overrides,
    })
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({
        "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(tmp_path / "cdk.out"),
        "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true",
    })
    return subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)


def templates(tmp_path):
    assembly = json.loads((tmp_path / "cdk.out/manifest.json").read_text())
    assert not assembly.get("missing"), "Offline synth must not require context lookups"
    return {path.stem.removesuffix(".template"): json.loads(path.read_text())
            for path in (tmp_path / "cdk.out").glob("*.template.json")}


def router_env(template):
    function = next(resource["Properties"] for resource in template["Resources"].values()
                    if resource["Type"] == "AWS::Lambda::Function"
                    and resource["Properties"].get("FunctionName") == "openclaw-router")
    return function["Environment"]["Variables"]


def as_list(value):
    return [value] if isinstance(value, str) else value


def test_synth_wires_allowlist_to_router_and_execution_role_covers_every_model(tmp_path):
    result = synth(tmp_path)
    assert result.returncode == 0, result.stderr
    built = templates(tmp_path)
    variables = router_env(built["OpenClawRouter"])
    assert json.loads(variables["USER_SELECTABLE_MODELS"]) == SHIPPED
    assert variables["DEFAULT_MODEL_ID"] == "global.anthropic.claude-opus-5-5"
    # Router stores the override on USER#/PROFILE via UpdateItem.
    router_actions = {action for resource in built["OpenClawRouter"]["Resources"].values()
                      if resource["Type"] == "AWS::IAM::Policy"
                      for statement in resource["Properties"]["PolicyDocument"]["Statement"]
                      for action in as_list(statement["Action"])}
    assert {"dynamodb:GetItem", "dynamodb:UpdateItem"} <= router_actions

    statements = [statement for logical_id, resource in built["OpenClawAgentCore"]["Resources"].items()
                  if resource["Type"] == "AWS::IAM::Policy" and logical_id.startswith("OpenClawExecutionRole")
                  for statement in resource["Properties"]["PolicyDocument"]["Statement"]]
    for model_id in SHIPPED.values():
        resources = [f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{model_id}"]
        resources += [f"arn:aws:bedrock:{region}::foundation-model/{model_id.removeprefix('global.')}"
                      for region in ("us-west-2", "us-east-1", "eu-west-1", "ap-southeast-2")]
        for action in ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"):
            for resource in resources:
                assert any(statement["Effect"] == "Allow" and action in as_list(statement["Action"])
                           and any(fnmatchcase(resource, pattern) for pattern in as_list(statement["Resource"]))
                           for statement in statements), (action, resource)


def test_synth_with_empty_map_disables_switching(tmp_path):
    result = synth(tmp_path, user_selectable_models={})
    assert result.returncode == 0, result.stderr
    assert router_env(templates(tmp_path)["OpenClawRouter"])["USER_SELECTABLE_MODELS"] == "{}"


@pytest.mark.parametrize("bad", [{"default": "global.anthropic.claude-haiku-5-5"}, {"opus": "not a model id"}, ["opus"]])
def test_synth_rejects_an_invalid_map(tmp_path, bad):
    result = synth(tmp_path, user_selectable_models=bad)
    assert result.returncode != 0
    assert "ValueError" in result.stderr and "user_selectable_models" in result.stderr
    assert not (tmp_path / "cdk.out/OpenClawRouter.template.json").exists()
