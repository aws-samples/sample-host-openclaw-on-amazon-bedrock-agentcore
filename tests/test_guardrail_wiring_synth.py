"""Synth-time regression test for Bedrock Guardrail wiring (issue #100).

Commit 923189c dropped ``guardrail_id=`` / ``guardrail_version=`` from the
``AgentCoreStack(...)`` call in ``app.py`` during a rebase. Because both
parameters default to ``""``, the ``bedrock:ApplyGuardrail`` statement silently
vanished from the runtime execution role while the OpenClawGuardrails stack and
the docs still said guardrails were on.

These tests build the same VpcStack -> SecurityStack -> GuardrailsStack ->
AgentCoreStack graph that ``app.py`` builds, with the same ``or ""`` wiring, and
assert on the synthesized IAM policy. No AWS calls: account/region and
availability zones are fixed via context so no CLI lookup is ever attempted.

    pytest tests/test_guardrail_wiring_synth.py -v
"""

from fnmatch import fnmatchcase
import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.agentcore_stack import AgentCoreStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.security_stack import SecurityStack
from stacks.vpc_stack import VpcStack

_ACCOUNT = "123456789012"
_REGION = "us-west-2"
_APPLY_GUARDRAIL_STATEMENT = {
    "Action": "bedrock:ApplyGuardrail",
    "Effect": "Allow",
    "Resource": f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:guardrail/*",
}


def _synth_agentcore(enable_guardrails: bool):
    """Mirror the app.py wiring and return (agentcore Template, guardrails Template|None)."""
    app = cdk.App(
        context={
            "account": _ACCOUNT,
            "region": _REGION,
            # Fixed AZs so VpcStack never triggers an availability-zone lookup.
            "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
            "enable_guardrails": enable_guardrails,
        }
    )
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)

    vpc_stack = VpcStack(app, "OpenClawVpc", env=env)
    security_stack = SecurityStack(app, "OpenClawSecurity", env=env)
    guardrails_stack = GuardrailsStack(
        app, "OpenClawGuardrails", cmk_arn=security_stack.cmk.key_arn, env=env
    )
    agentcore_stack = AgentCoreStack(
        app,
        "OpenClawAgentCore",
        cmk_arn=security_stack.cmk.key_arn,
        vpc=vpc_stack.vpc,
        private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
        cognito_issuer_url=security_stack.cognito_issuer_url,
        cognito_client_id=security_stack.user_pool_client_id,
        cognito_user_pool_id=security_stack.user_pool_id,
        cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
        gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
        # Same expression as app.py — this is the line #30 dropped.
        guardrail_id=guardrails_stack.guardrail_id or "",
        guardrail_version=guardrails_stack.guardrail_version or "",
        env=env,
    )

    agentcore_tpl = Template.from_stack(agentcore_stack)
    guardrails_tpl = Template.from_stack(guardrails_stack)
    return agentcore_tpl, guardrails_tpl


def _apply_guardrail_statements(template: Template) -> list:
    """Return every IAM statement in the template granting bedrock:ApplyGuardrail."""
    found = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        for st in res["Properties"]["PolicyDocument"].get("Statement", []):
            actions = st.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "bedrock:ApplyGuardrail" in actions:
                found.append(st)
    return found


def test_runtime_role_has_apply_guardrail_when_enabled():
    """With enable_guardrails=true (the default) the execution role must be able
    to apply guardrails, otherwise the proxy's guardrailConfig is rejected."""
    agentcore_tpl, _ = _synth_agentcore(enable_guardrails=True)

    agentcore_tpl.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([_APPLY_GUARDRAIL_STATEMENT]),
            },
        },
    )
    assert len(_apply_guardrail_statements(agentcore_tpl)) == 1


def test_guardrails_stack_exposes_fixed_output_keys_when_enabled():
    """scripts/deploy.sh Phase 2 looks these up with exact OutputKey== matches;
    renaming them would silently hand the runtime an empty BEDROCK_GUARDRAIL_ID."""
    _, guardrails_tpl = _synth_agentcore(enable_guardrails=True)
    outputs = guardrails_tpl.to_json().get("Outputs", {})
    assert {"GuardrailId", "GuardrailVersion"} <= set(outputs), sorted(outputs)


def test_runtime_role_has_no_apply_guardrail_when_disabled():
    """With enable_guardrails=false the GuardrailsStack exports None and the
    AgentCoreStack must skip the ApplyGuardrail grant cleanly."""
    agentcore_tpl, guardrails_tpl = _synth_agentcore(enable_guardrails=False)

    assert _apply_guardrail_statements(agentcore_tpl) == []
    guardrails_tpl.resource_count_is("AWS::Bedrock::Guardrail", 0)
    assert "GuardrailId" not in guardrails_tpl.to_json().get("Outputs", {})


def test_default_primary_and_fallback_keep_global_streaming_invocation_grants():
    """Global profile invocation needs both profile and destination model ARNs."""
    context = json.loads((Path(__file__).resolve().parents[1] / "cdk.json").read_text())["context"]
    template, _ = _synth_agentcore(enable_guardrails=True)
    statements = [
        statement
        for logical_id, resource in template.find_resources("AWS::IAM::Policy").items()
        if logical_id.startswith("OpenClawExecutionRole")
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]
    for model_id in [context["default_model_id"], context["fallback_model_id"]]:
        assert model_id.startswith("global.")
        resources = [f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:inference-profile/{model_id}"]
        resources.extend(
            f"arn:aws:bedrock:{region}::foundation-model/{model_id.removeprefix('global.')}"
            for region in ["us-west-2", "us-east-1", "eu-west-1"]
        )
        for action in ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]:
            for resource in resources:
                assert any(
                    statement["Effect"] == "Allow"
                    and action in (
                        [statement["Action"]] if isinstance(statement["Action"], str)
                        else statement["Action"]
                    )
                    and any(fnmatchcase(resource, pattern) for pattern in (
                        [statement["Resource"]] if isinstance(statement["Resource"], str)
                        else statement["Resource"]
                    ))
                    for statement in statements
                ), (action, resource)


def _execution_role_log_statements(template: Template) -> list:
    policies = [
        resource for logical_id, resource in template.find_resources("AWS::IAM::Policy").items()
        if logical_id.startswith("OpenClawExecutionRole")
    ]
    assert len(policies) == 1
    statements = []
    for statement in policies[0]["Properties"]["PolicyDocument"]["Statement"]:
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        if any(action.startswith("logs:") for action in actions):
            statements.append(statement)
    return statements


def test_runtime_native_log_permissions_do_not_depend_on_runtime_name_or_id():
    prefix = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:"
    expected = {
        frozenset(["logs:CreateLogGroup", "logs:DescribeLogStreams"]):
            prefix + "/aws/bedrock-agentcore/runtimes/*",
        frozenset(["logs:CreateLogStream", "logs:PutLogEvents"]):
            prefix + "/aws/bedrock-agentcore/runtimes/*:log-stream:*",
        frozenset(["logs:DescribeLogGroups"]): prefix + "*",
    }
    template, _ = _synth_agentcore(enable_guardrails=False)
    statements = _execution_role_log_statements(template)
    assert len(statements) == 4
    actual = {}
    for statement in statements:
        if statement["Resource"] == [prefix + "/openclaw/*", prefix + "/openclaw/*:*"]:
            continue
        assert statement["Effect"] == "Allow"
        assert "Condition" not in statement
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        actual[frozenset(actions)] = statement["Resource"]
    assert actual == expected


def test_runtime_custom_log_permissions_are_preserved():
    template, _ = _synth_agentcore(enable_guardrails=False)
    prefix = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:"
    statements = _execution_role_log_statements(template)
    custom = [
        statement for statement in statements
        if statement["Resource"] == [prefix + "/openclaw/*", prefix + "/openclaw/*:*"]
    ]
    assert custom == [{
        "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        "Effect": "Allow",
        "Resource": [prefix + "/openclaw/*", prefix + "/openclaw/*:*"],
    }]


def test_runtime_native_log_nag_suppressions_are_resource_scoped():
    template, _ = _synth_agentcore(enable_guardrails=False)
    role = next(
        resource for logical_id, resource in template.find_resources("AWS::IAM::Role").items()
        if logical_id.startswith("OpenClawExecutionRole")
    )
    suppressions = [
        suppression for suppression in role["Metadata"]["cdk_nag"]["rules_to_suppress"]
        if suppression["id"] == "AwsSolutions-IAM5"
    ]
    assert len(suppressions) == 1
    entries = suppressions[0]["applies_to"]
    prefix = f"Resource::arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:"
    assert {entry for entry in entries if entry.startswith("Resource::arn:aws:logs:")} == {
        prefix + "/openclaw/*",
        prefix + "/openclaw/*:*",
        prefix + "/aws/bedrock-agentcore/runtimes/*",
        prefix + "/aws/bedrock-agentcore/runtimes/*:log-stream:*",
        prefix + "*",
    }
    assert not any(entry.startswith("Action::logs:") for entry in entries)
