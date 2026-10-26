"""Synth-time tests for service-principal trust policies (no AWS calls).

- OpenClawExecutionRole is assumed by AgentCore for the Runtime and, when
  enable_browser is set, for the custom Browser, which uses the same role. Its
  trust is limited to this account and to AgentCore resources in this account
  and Region, and must admit both resource types.
- CronSchedulerRole is assumed by EventBridge Scheduler for this account only.
- gateway_debug_errors is honoured both as a JSON boolean from cdk.json and as
  the string a ``-c gateway_debug_errors=true`` flag passes.

    pytest tests/test_trust_policies_synth.py -v
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.gateway_stack import GatewayStack
from stacks.security_stack import SecurityStack
from tests._app_synth import ACCOUNT, REGION, iam_pattern_matches, resources, synth_app


@pytest.fixture(scope="module")
def templates():
    return synth_app(enable_browser=True)


def _role(templates, stack, logical_prefix):
    roles = [res for _, lid, res in resources({stack: templates[stack]}, "AWS::IAM::Role")
             if lid.startswith(logical_prefix)]
    assert len(roles) == 1, f"expected one {logical_prefix} in {stack}"
    return roles[0]


def _service_statements(role, service):
    return [s for s in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
            if s.get("Principal", {}).get("Service") == service]


def _as_list(value):
    return value if isinstance(value, list) else [value]


def test_execution_role_trusts_only_agentcore_with_account_and_arn_conditions(templates):
    role = _role(templates, "OpenClawAgentCore", "OpenClawExecutionRole")
    services = [s["Principal"]["Service"]
                for s in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
                if "Service" in s.get("Principal", {})]
    assert services == ["bedrock-agentcore.amazonaws.com"]
    (stmt,) = _service_statements(role, "bedrock-agentcore.amazonaws.com")
    assert stmt["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    assert set(stmt["Condition"]) == {"StringEquals", "ArnLike"}


@pytest.mark.parametrize("source_arn", [
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/openclaw_agent-AbCdEf1234",
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:browser-custom/openclaw_browser-AbCdEf1234",
])
def test_execution_role_admits_runtime_and_browser(templates, source_arn):
    role = _role(templates, "OpenClawAgentCore", "OpenClawExecutionRole")
    (stmt,) = _service_statements(role, "bedrock-agentcore.amazonaws.com")
    patterns = _as_list(stmt["Condition"]["ArnLike"]["aws:SourceArn"])
    assert any(iam_pattern_matches(p, source_arn) for p in patterns), patterns


@pytest.mark.parametrize("source_arn", [
    f"arn:aws:bedrock-agentcore:{REGION}:999999999999:runtime/other-AbCdEf1234",
    f"arn:aws:bedrock-agentcore:eu-west-1:{ACCOUNT}:runtime/other-AbCdEf1234",
    f"arn:aws:bedrock:{REGION}:{ACCOUNT}:agent/AbCdEf1234",
])
def test_execution_role_refuses_other_accounts_regions_and_services(templates, source_arn):
    role = _role(templates, "OpenClawAgentCore", "OpenClawExecutionRole")
    (stmt,) = _service_statements(role, "bedrock-agentcore.amazonaws.com")
    patterns = _as_list(stmt["Condition"]["ArnLike"]["aws:SourceArn"])
    assert not any(iam_pattern_matches(p, source_arn) for p in patterns), patterns


def test_browser_uses_the_execution_role(templates):
    browsers = [res for _, _, res in resources(templates, "AWS::BedrockAgentCore::BrowserCustom")]
    assert len(browsers) == 1
    role_ref = browsers[0]["Properties"]["ExecutionRoleArn"]["Fn::GetAtt"][0]
    assert role_ref.startswith("OpenClawExecutionRole")


def test_scheduler_role_trusts_scheduler_for_this_account_only(templates):
    role = _role(templates, "OpenClawCron", "CronSchedulerRole")
    statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert [s["Principal"] for s in statements] == [{"Service": "scheduler.amazonaws.com"}]
    assert statements[0]["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}


def _gateway_exception_level(**context):
    app = cdk.App(context={
        "account": ACCOUNT, "region": REGION,
        "availability_zones": [f"{REGION}a", f"{REGION}b"],
        "enable_gateway": True, **context,
    })
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    security = SecurityStack(app, "OpenClawSecurity", env=env)
    gateway = GatewayStack(
        app, "OpenClawGateway",
        cognito_issuer_url=security.cognito_issuer_url,
        cognito_client_id=security.user_pool_client_id,
        cmk_arn=security.cmk.key_arn,
        env=env,
    )
    (props,) = [g["Properties"] for g in
                Template.from_stack(gateway).find_resources("AWS::BedrockAgentCore::Gateway").values()]
    return props.get("ExceptionLevel")


@pytest.mark.parametrize("value", [True, "true", "True", " TRUE "])
def test_gateway_debug_errors_accepts_boolean_and_cli_string(value):
    assert _gateway_exception_level(gateway_debug_errors=value) == "DEBUG"


@pytest.mark.parametrize("value", [None, False, "false", "", "0", "yes"])
def test_gateway_debug_errors_off_unless_true(value):
    context = {} if value is None else {"gateway_debug_errors": value}
    assert _gateway_exception_level(**context) is None
