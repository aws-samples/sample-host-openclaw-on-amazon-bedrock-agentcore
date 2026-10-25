import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from stacks.runtime_config import load_runtime_manifest, runtime_target_from_manifest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, REGION = "123456789012", "us-west-2"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/legacy-123"
V2_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/candidate-123"


def manifest():
    nonce = "c" * 32
    session = f"deployment-warmup-{nonce}"
    return {
        "schemaVersion": 1, "agentRuntimeId": "candidate-123", "agentRuntimeArn": V2_ARN,
        "agentRuntimeVersion": "7", "platformVersion": "V2", "endpointName": "release_1",
        "imageUri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/openclaw-bridge@sha256:" + "a" * 64,
        "imageDigest": "sha256:" + "a" * 64, "configurationHash": "b" * 64,
        "endpointPolicy": "IMMUTABLE",
        "warmup": {
            "mode": "chat", "status": "succeeded", "contractVersion": 1,
            "runtimePlatform": "V2", "phase": "USER_READY",
            "execution": {"engine": "openclaw", "phase": "completed"},
            "userId": session, "sessionId": session, "runtimeSessionId": session,
            "actorId": f"deployment-warmup:{nonce}",
        },
    }


def run_app(tmp_path, runtime=None, expect_failure=False, context_overrides=None):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
        "runtime_manifest": "",
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
    })
    context.update(context_overrides or {})
    if runtime:
        path = tmp_path / "runtime.json"
        path.write_text(json.dumps(runtime))
        context["runtime_manifest"] = str(path)
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({
        "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(tmp_path / "cdk.out"),
        "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION,
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    if expect_failure:
        assert result.returncode != 0, "Invalid enrollment manifest reached CDK synth"
        assert "ValueError:" in result.stderr, result.stderr
        assert not (tmp_path / "cdk.out/OpenClawRouter.template.json").exists()
        return
    assert result.returncode == 0, result.stderr
    assembly = json.loads((tmp_path / "cdk.out/manifest.json").read_text())
    assert not assembly.get("missing"), "Offline synth must not require context lookups"
    return {path.name: json.loads(path.read_text())
            for path in (tmp_path / "cdk.out").glob("*.template.json")}


def invoke_resources(template):
    statements = [
        statement for resource in template["Resources"].values() if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        if statement["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    ]
    assert len(statements) <= 1
    return set(statements[0]["Resource"]) if statements else set()


@pytest.mark.parametrize("runtime_enabled", [False, True])
def test_single_v2_runtime_scopes_both_callers_iam_and_packages_shared_layer(tmp_path, runtime_enabled):
    templates = run_app(tmp_path, manifest() if runtime_enabled else None)
    assert len(templates) == 8
    for name in ("OpenClawRouter", "OpenClawCron"):
        template = templates[f"{name}.template.json"]
        functions = [r["Properties"] for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Function"]
        function = next(f for f in functions if f.get("FunctionName") in ("openclaw-router", "openclaw-cron-executor"))
        assert function["Environment"]["Variables"]["AGENTCORE_RUNTIME_ARN"] == (V2_ARN if runtime_enabled else "")
        assert function["Environment"]["Variables"]["AGENTCORE_QUALIFIER"] == ("release_1" if runtime_enabled else "")
        if name == "OpenClawRouter":
            assert json.loads(function["Environment"]["Variables"]["RUNTIME_TARGET"]) == (
                runtime_target_from_manifest(manifest()) if runtime_enabled else None
            )
            actions = {
                action
                for resource in template["Resources"].values() if resource["Type"] == "AWS::IAM::Policy"
                for statement in resource["Properties"]["PolicyDocument"]["Statement"]
                for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
            }
            assert {"dynamodb:Scan", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"} <= actions
        assert len(function["Layers"]) == 1
        layer_id = function["Layers"][0]["Ref"]
        assert template["Resources"][layer_id]["Type"] == "AWS::Lambda::LayerVersion"
        expected = {V2_ARN, V2_ARN + "/*"} if runtime_enabled else set()
        assert invoke_resources(template) == expected
    assert not any(r["Type"] == "AWS::BedrockAgentCore::Runtime"
                   for template in templates.values() for r in template["Resources"].values())
    layers = list((tmp_path / "cdk.out").glob("asset.*/python/runtime_routing.py"))
    assert layers and all(path.read_bytes() == (ROOT / "lambda/shared/python/runtime_routing.py").read_bytes()
                          for path in layers)


def test_manifest_endpoint_reaches_both_callers(tmp_path):
    templates = run_app(tmp_path, {**manifest(), "endpointName": "release_2"})
    for name in ("OpenClawRouter", "OpenClawCron"):
        functions = templates[f"{name}.template.json"]["Resources"].values()
        function = next(r["Properties"] for r in functions if r["Type"] == "AWS::Lambda::Function")
        assert function["Environment"]["Variables"]["AGENTCORE_QUALIFIER"] == "release_2"


@pytest.mark.parametrize("change", [
    {"schemaVersion": 2}, {"schemaVersion": True}, {"platformVersion": "V1"},
    {"agentRuntimeArn": ARN}, {"endpointName": V2_ARN}, {"endpointName": "candidate-id"},
    {"agentRuntimeId": "other"}, {"agentRuntimeArn": V2_ARN.replace(ACCOUNT, "000000000000")},
])
def test_runtime_manifest_mismatch_stops_synth_input(tmp_path, change):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({**manifest(), **change}))
    with pytest.raises(ValueError):
        load_runtime_manifest(path, REGION, ACCOUNT)


def test_missing_manifest_allows_foundation_without_registration_target():
    assert load_runtime_manifest("", REGION, ACCOUNT) is None
    assert runtime_target_from_manifest(None) is None


def test_manifest_alone_pins_verified_v2_target(tmp_path):
    templates = run_app(tmp_path, manifest())
    for name in ("OpenClawRouter", "OpenClawCron"):
        template = templates[f"{name}.template.json"]
        function = next(r["Properties"] for r in template["Resources"].values()
                        if r["Type"] == "AWS::Lambda::Function")
        env = function["Environment"]["Variables"]
        assert env["AGENTCORE_RUNTIME_ARN"] == V2_ARN
        expected_arns = {V2_ARN, V2_ARN + "/*"}
        assert invoke_resources(template) == expected_arns
        if name == "OpenClawRouter":
            target = json.loads(env["RUNTIME_TARGET"])
            assert target == runtime_target_from_manifest(manifest())
            assert target["runtimeVersion"] == "7"
        else:
            assert "RUNTIME_TARGET" not in env


@pytest.mark.parametrize("change", [
    {"platformVersion": "V1"},
    {"warmup": {"mode": "chat", "status": "skipped"}},
    {"warmup": {"mode": "probe", "status": "succeeded"}},
    {"warmup": {"mode": "chat", "status": "failed"}},
    {"endpointName": "DEFAULT"}, {"agentRuntimeVersion": ""},
    {"imageDigest": "latest"}, {"configurationHash": ""}, {"imageUri": "image:tag"},
])
def test_unverified_manifest_cannot_enable_new_registration(change):
    with pytest.raises(ValueError):
        runtime_target_from_manifest({**manifest(), **change})


@pytest.mark.parametrize("policy", [None, "MUTABLE"])
def test_enrollment_requires_immutable_endpoint_policy(policy):
    candidate = manifest()
    if policy is None:
        del candidate["endpointPolicy"]
    else:
        candidate["endpointPolicy"] = policy
    with pytest.raises(ValueError):
        runtime_target_from_manifest(candidate)


def test_legacy_success_record_cannot_enroll_or_reach_synth(tmp_path):
    candidate = manifest()
    candidate["warmup"] = {"mode": "chat", "status": "succeeded"}
    with pytest.raises(ValueError):
        runtime_target_from_manifest(candidate)
    path = tmp_path / "legacy-runtime.json"
    path.write_text(json.dumps(candidate))
    with pytest.raises(ValueError):
        load_runtime_manifest(path, REGION, ACCOUNT)


@pytest.mark.parametrize("change", [
    {"contractVersion": None}, {"contractVersion": True}, {"contractVersion": 2},
    {"runtimePlatform": "V1"}, {"phase": "BOOTSTRAP_READY"},
    {"execution": {"engine": "lightweight", "phase": "completed"}},
    {"execution": {"engine": "openclaw", "phase": "unknown_outcome"}},
    {"execution": None}, {"userId": "other-owner"}, {"actorId": "telegram:123"},
    {"sessionId": "other-session"}, {"runtimeSessionId": "other-session"},
])
def test_enrollment_rejects_incomplete_gateway_or_mismatched_binding_evidence(change):
    candidate = manifest()
    candidate["warmup"].update(change)
    with pytest.raises(ValueError):
        runtime_target_from_manifest(candidate)


@pytest.mark.parametrize("kind", ["legacy", "shim", "binding"])
def test_direct_cdk_synth_rejects_unsafe_enrollment_evidence(tmp_path, kind):
    candidate = manifest()
    if kind == "legacy":
        candidate["warmup"] = {"mode": "chat", "status": "succeeded"}
    elif kind == "shim":
        candidate["warmup"]["execution"]["engine"] = "lightweight"
    else:
        candidate["warmup"]["actorId"] = "telegram:123"
    run_app(tmp_path, candidate, expect_failure=True)


def test_cdk_context_has_one_runtime_manifest_and_no_v1_defaults():
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    assert context["runtime_manifest"] == ""
    assert not {
        "candidate_runtime_manifest", "enable_v2_new_registrations", "enable_v1_fallback",
        "runtime_id", "runtime_endpoint_id", "runtime_endpoint_name",
    } & context.keys()
