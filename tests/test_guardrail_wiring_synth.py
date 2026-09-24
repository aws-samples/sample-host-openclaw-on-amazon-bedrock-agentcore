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
            "runtime_id": "openclaw_agent-TEST",
            "runtime_endpoint_id": "DEFAULT",
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
