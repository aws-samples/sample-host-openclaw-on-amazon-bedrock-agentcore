"""Synth-time tests for the opt-in AgentCore Gateway MCP tools (stacks/gateway_stack.py).

No AWS calls: account/region/AZs are fixed via context.

    pytest tests/test_gateway_stack_synth.py -v
"""

import json
import pathlib
import subprocess
import sys

import aws_cdk as cdk
import cdk_nag
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from stacks.gateway_stack import GatewayStack, load_tool_schemas
from stacks.security_stack import SecurityStack

_ACCOUNT = "123456789012"
_REGION = "us-west-2"
_REPO = pathlib.Path(__file__).resolve().parent.parent


def _base_context(**extra):
    ctx = {
        "account": _ACCOUNT,
        "region": _REGION,
        "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
        "runtime_id": "openclaw_agent-TEST",
        "runtime_endpoint_id": "DEFAULT",
    }
    ctx.update(extra)
    return ctx


def _synth_gateway():
    app = cdk.App(context=_base_context(enable_gateway=True))
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    security = SecurityStack(app, "OpenClawSecurity", env=env)
    gateway = GatewayStack(
        app,
        "OpenClawGateway",
        cognito_issuer_url=security.cognito_issuer_url,
        cognito_client_id=security.user_pool_client_id,
        cmk_arn=security.cmk.key_arn,
        env=env,
    )
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))
    return gateway, Template.from_stack(gateway)


def _run_app(tmp_path: pathlib.Path, enable_gateway):
    """Run app.py exactly as `cdk synth` would, hermetically, and return the set of stack names."""
    cdk_json = json.loads((_REPO / "cdk.json").read_text())
    ctx = dict(cdk_json["context"])
    ctx.update(_base_context())
    if enable_gateway is not None:
        ctx["enable_gateway"] = enable_gateway
    else:
        ctx.pop("enable_gateway", None)
    out = tmp_path / ("on" if enable_gateway else "off")
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", "/tmp"),
        "CDK_OUTDIR": str(out),
        "CDK_CONTEXT_JSON": json.dumps(ctx),
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DEFAULT_ACCOUNT": _ACCOUNT,
        "CDK_DEFAULT_REGION": _REGION,
    }
    for k in ("JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR", "NODE_OPTIONS"):
        v = __import__("os").environ.get(k)
        if v:
            env[k] = v
    subprocess.run([sys.executable, "app.py"], cwd=_REPO, env=env, check=True, capture_output=True)
    return {p.name[: -len(".template.json")] for p in out.glob("*.template.json")}, out


# --------------------------------------------------------------------------- flag off


def test_flag_off_creates_no_gateway_stack(tmp_path):
    stacks, _ = _run_app(tmp_path, enable_gateway=False)
    assert "OpenClawGateway" not in stacks
    assert len(stacks) == 8


def test_flag_missing_behaves_as_off(tmp_path):
    stacks, _ = _run_app(tmp_path, enable_gateway=None)
    assert "OpenClawGateway" not in stacks


def test_flag_off_leaves_existing_templates_untouched(tmp_path):
    """With the flag off no template carries Gateway resources or outputs.

    Byte-identity of the 8 existing templates against main is checked in the PR
    (diff of two hermetic synths); this is the in-tree structural guard.
    """
    off_stacks, off_dir = _run_app(tmp_path, enable_gateway=False)
    for name in off_stacks:
        tpl = json.loads((off_dir / f"{name}.template.json").read_text())
        assert "GatewayUrl" not in tpl.get("Outputs", {}), name
        for res in tpl["Resources"].values():
            assert not res["Type"].startswith("AWS::BedrockAgentCore::Gateway"), name


# --------------------------------------------------------------------------- flag on


def test_flag_on_adds_gateway_stack(tmp_path):
    stacks, out = _run_app(tmp_path, enable_gateway=True)
    assert "OpenClawGateway" in stacks
    assert len(stacks) == 9
    tpl = json.loads((out / "OpenClawGateway.template.json").read_text())
    assert set(tpl["Outputs"]) >= {"GatewayUrl", "GatewayId"}


def test_gateway_uses_custom_jwt_against_existing_user_pool():
    _, tpl = _synth_gateway()
    tpl.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)
    tpl.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {
            "Name": "openclaw-tools",
            "ProtocolType": "MCP",
            "AuthorizerType": "CUSTOM_JWT",
            "AuthorizerConfiguration": {
                "CustomJWTAuthorizer": {
                    # Discovery URL is built from the OpenClawSecurity pool id.
                    "DiscoveryUrl": Match.object_like(
                        {"Fn::Join": ["", Match.array_with(["/.well-known/openid-configuration"])]}
                    ),
                    # Only the proxy's app client (whose ID token the proxy mints).
                    "AllowedClients": [Match.any_value()],
                }
            },
            "InterceptorConfigurations": [
                Match.object_like(
                    {
                        "InterceptionPoints": ["REQUEST"],
                        "InputConfiguration": {"PassRequestHeaders": True},
                    }
                )
            ],
        },
    )


def test_two_lambda_targets_with_declared_tools():
    _, tpl = _synth_gateway()
    tpl.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 2)
    schemas = load_tool_schemas()
    targets = tpl.find_resources("AWS::BedrockAgentCore::GatewayTarget")
    by_name = {t["Properties"]["Name"]: t for t in targets.values()}
    assert set(by_name) == {"user-files", "schedules"}
    for name, target in by_name.items():
        props = target["Properties"]
        assert props["CredentialProviderConfigurations"] == [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}]
        tools = props["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        assert [t["Name"] for t in tools] == [t["name"] for t in schemas[name]["tools"]]
        for tool in tools:
            # Every tool carries the reserved identity slot the interceptor fills.
            assert "__caller_token" in tool["InputSchema"]["Properties"], tool["Name"]
            assert "__caller_token" not in tool["InputSchema"].get("Required", [])
            # No user_id / namespace argument is offered to the model.
            assert not ({"user_id", "namespace", "actor_id"} & set(tool["InputSchema"]["Properties"]))


def test_tool_schemas_match_lambda_handlers():
    """Tool names in the JSON must be the ones the handlers dispatch on (simple grep-level guard)."""
    schemas = load_tool_schemas()
    files_src = (_REPO / "lambda/gateway_tools/s3_user_files/index.js").read_text()
    cron_src = (_REPO / "lambda/gateway_tools/eventbridge_cron/index.js").read_text()
    for t in schemas["user-files"]["tools"]:
        assert f"async {t['name']}(" in files_src, t["name"]
    for t in schemas["schedules"]["tools"]:
        assert f"async {t['name']}(" in cron_src, t["name"]


def _statements(tpl: Template, fn_logical_prefix: str):
    out = []
    for lid, res in tpl.find_resources("AWS::IAM::Policy").items():
        if lid.startswith(fn_logical_prefix):
            out.extend(res["Properties"]["PolicyDocument"]["Statement"])
    return out


def test_lambda_iam_is_scoped():
    _, tpl = _synth_gateway()
    tpl.resource_count_is("AWS::Lambda::Function", 3)
    for res in tpl.find_resources("AWS::Lambda::Function").values():
        assert res["Properties"]["Runtime"] == "nodejs22.x"

    files = _statements(tpl, "UserFilesFn")
    actions = {a for st in files for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])}
    assert actions == {"s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "kms:Decrypt", "kms:GenerateDataKey"}
    for st in files:
        for r in [st["Resource"]] if not isinstance(st["Resource"], list) else st["Resource"]:
            assert r != "*"
            if isinstance(r, str):
                assert f"openclaw-user-files-{_ACCOUNT}-{_REGION}" in r

    sched = _statements(tpl, "SchedulesFn")
    actions = {a for st in sched for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])}
    assert actions == {
        "scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule",
        "iam:PassRole",
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
    }
    pass_role = next(st for st in sched if "iam:PassRole" in st["Action"])
    assert pass_role["Condition"] == {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}
    assert pass_role["Resource"] == f"arn:aws:iam::{_ACCOUNT}:role/openclaw-cron-scheduler-role-{_REGION}"
    sched_stmt = next(st for st in sched if "scheduler:CreateSchedule" in st["Action"])
    assert sched_stmt["Resource"] == f"arn:aws:scheduler:{_REGION}:{_ACCOUNT}:schedule/openclaw-cron/*"
    # Neither tool Lambda may list or read the whole table / other buckets.
    assert "s3:*" not in actions and "dynamodb:Scan" not in actions


def test_gateway_role_can_only_invoke_the_three_functions_and_use_the_cmk():
    _, tpl = _synth_gateway()
    stmts = _statements(tpl, "GatewayServiceRole")
    by_action = {json.dumps(st["Action"], sort_keys=True): st for st in stmts}
    assert len(stmts) == 4, by_action.keys()
    invoke = next(st for st in stmts if st["Action"] == "lambda:InvokeFunction")
    assert len(invoke["Resource"]) == 3
    # The Gateway encrypts target config with the CMK via this role; every KMS
    # statement is pinned to the one key, to the AgentCore service, and (for the
    # data-key and grant statements) to a gateway ARN in this account/region.
    kms_stmts = [st for st in stmts if st is not invoke]
    assert sorted(st["Sid"] for st in kms_stmts) == ["GatewayCmkDataKeys", "GatewayCmkDescribe", "GatewayCmkGrant"]
    for st in kms_stmts:
        assert all(a.startswith("kms:") for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"]))
        assert st["Resource"] != "*"
        assert st["Condition"]["StringEquals"]["kms:ViaService"] == f"bedrock-agentcore.{_REGION}.amazonaws.com"
        if st["Sid"] != "GatewayCmkDescribe":
            ctx = st["Condition"]["StringLike"]["kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn"]
            assert ctx == f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:gateway/*"
    grant = next(st for st in kms_stmts if st["Sid"] == "GatewayCmkGrant")
    assert grant["Condition"]["ForAllValues:StringEquals"]["kms:GrantOperations"] == ["Decrypt", "GenerateDataKey"]
    assert grant["Condition"]["StringEquals"]["kms:GrantConstraintType"] == "EncryptionContextSubset"
    roles = tpl.find_resources("AWS::IAM::Role")
    gw_role = next(r for lid, r in roles.items() if lid.startswith("GatewayServiceRole"))
    trust = gw_role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": _ACCOUNT}


def test_cdk_nag_has_no_errors():
    stack, _ = _synth_gateway()
    errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp("AwsSolutions-.*"))
    assert errors == [], [e.entry.data for e in errors]
