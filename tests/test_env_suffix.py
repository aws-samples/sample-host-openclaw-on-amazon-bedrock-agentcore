"""Environment-suffix naming (environment_suffix context / OPENCLAW_ENV_SUFFIX).

An empty suffix must leave every name unchanged; a suffix must reach every
physical name the stacks and deployment scripts share. The whole app.py is
synthesized in a child process with fixed account/region/AZs, so no AWS calls.

    pytest tests/test_env_suffix.py -v
"""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from stacks import DeploymentNamer, environment_suffix  # noqa: E402
import env_suffix  # noqa: E402
import runtime_deploy  # noqa: E402
from runtime_config import foundation_configuration  # noqa: E402

ACCOUNT, REGION = "123456789012", "us-west-2"
STACKS = ["OpenClawVpc", "OpenClawSecurity", "OpenClawGuardrails", "OpenClawGateway", "OpenClawAgentCore",
          "OpenClawRouter", "OpenClawCron", "OpenClawObservability", "OpenClawTokenMonitoring", "OpenClawBuild"]


def _scope(value):
    return SimpleNamespace(node=SimpleNamespace(try_get_context=lambda key: value))


@pytest.fixture(autouse=True)
def no_suffix_env(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ENV_SUFFIX", raising=False)


def test_empty_suffix_keeps_names():
    namer = DeploymentNamer()
    assert namer.stack("OpenClawRouter") == "OpenClawRouter"
    assert namer.name("openclaw/channels/telegram") == "openclaw/channels/telegram"
    assert namer.runtime_name("openclaw_browser") == "openclaw_browser"
    for value in (None, "", "  ", "-"):
        assert environment_suffix(_scope(value)) == ""


def test_suffix_forms():
    namer = DeploymentNamer("team-a")
    assert namer.stack("OpenClawRouter") == "OpenClawRouter-team-a"
    assert namer.name("openclaw-identity") == "openclaw-identity-team-a"
    assert namer.runtime_name("openclaw_browser") == "openclaw_browser_team_a"
    assert environment_suffix(_scope(" Dev ")) == "dev"


@pytest.mark.parametrize("value", ["dev_1", "a--b", "x" * 13, "dév"])
def test_invalid_suffix_is_rejected(value):
    with pytest.raises(ValueError):
        environment_suffix(_scope(value))


def test_environment_variable_overrides_context(monkeypatch):
    monkeypatch.setenv("OPENCLAW_ENV_SUFFIX", "prod")
    assert environment_suffix(_scope("dev")) == "prod"


def test_script_helper_matches_stacks_helper():
    for value in ("", "dev", "team-a"):
        environ = {"OPENCLAW_ENV_SUFFIX": value}
        assert env_suffix.with_suffix("openclaw-cron", environ) == DeploymentNamer(value).name("openclaw-cron")
        assert (env_suffix.with_suffix("openclaw_agent_v2", environ, "_")
                == DeploymentNamer(value).runtime_name("openclaw_agent_v2"))
    with pytest.raises(ValueError):
        env_suffix.env_suffix({"OPENCLAW_ENV_SUFFIX": "bad_name"})


def _foundation(**extra):
    values = {
        "EXECUTION_ROLE_ARN": "arn:aws:iam::123456789012:role/r", "SECURITY_GROUP_ID": "sg-1",
        "PRIVATE_SUBNET_IDS": "subnet-1", "USER_FILES_BUCKET": "b", "COGNITO_USER_POOL_ID": "p",
        "COGNITO_CLIENT_ID": "c", "CMK_ARN": "k", "REGION": REGION, "ACCOUNT": ACCOUNT,
        "SESSION_IDLE": "1800", "SESSION_MAX": "28800", **extra,
    }
    return foundation_configuration(values)["environmentVariables"]


def test_runtime_configuration_names_follow_the_suffix():
    plain, dev = _foundation(), _foundation(OPENCLAW_ENV_SUFFIX="dev")
    assert plain["EVENTBRIDGE_SCHEDULE_GROUP"] == "openclaw-cron"
    assert plain["IDENTITY_TABLE_NAME"] == "openclaw-identity"
    assert plain["CRON_LAMBDA_ARN"].endswith(":function:openclaw-cron-executor")
    assert plain["EVENTBRIDGE_ROLE_ARN"].endswith(f":role/openclaw-cron-scheduler-role-{REGION}")
    assert dev["EVENTBRIDGE_SCHEDULE_GROUP"] == "openclaw-cron-dev"
    assert dev["IDENTITY_TABLE_NAME"] == "openclaw-identity-dev"
    assert dev["CRON_LAMBDA_ARN"].endswith(":function:openclaw-cron-executor-dev")
    assert dev["EVENTBRIDGE_ROLE_ARN"].endswith(f":role/openclaw-cron-scheduler-role-{REGION}-dev")


def test_existing_caller_check_reads_suffixed_functions():
    seen = []

    def read(FunctionName):
        seen.append(FunctionName)
        arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1"
        return {"Environment": {"Variables": {
            "AGENTCORE_RUNTIME_ARN": arn,
            "RUNTIME_TARGET": json.dumps({"platformVersion": "V2", "runtimeArn": arn}),
        }}}

    manifest = {"agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/rt-1"}
    runtime_deploy.verify_existing_callers(SimpleNamespace(get_function_configuration=read), manifest,
                                           {"OPENCLAW_ENV_SUFFIX": "dev"})
    assert seen == ["openclaw-router-dev", "openclaw-cron-executor-dev"]


def _synth(tmp_path, **context_overrides):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({"account": ACCOUNT, "region": REGION,
                    "availability_zones": [f"{REGION}a", f"{REGION}b"], "enable_gateway": True,
                    "enable_codebuild": True})
    context.update(context_overrides)
    outdir = tmp_path / "cdk.out"
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_ENV_SUFFIX"}
    env.update(CDK_CONTEXT_JSON=json.dumps(context), CDK_OUTDIR=str(outdir))
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-4000:]
    return {p.name.removesuffix(".template.json"): json.loads(p.read_text())
            for p in outdir.glob("*.template.json")}


@pytest.fixture(scope="module")
def dev_templates(tmp_path_factory):
    return _synth(tmp_path_factory.mktemp("dev"), environment_suffix="dev")


def _physical_names(templates):
    keys = ("TableName", "FunctionName", "RoleName", "BucketName", "Name", "AliasName", "LogGroupName",
            "AlarmName", "DashboardName", "TopicName", "StateMachineName", "UserPoolName", "ClientName",
            "ProjectName")
    for stack, template in templates.items():
        for logical_id, res in template.get("Resources", {}).items():
            for key in keys:
                value = res.get("Properties", {}).get(key)
                if isinstance(value, str) and "openclaw" in value.lower():
                    yield stack, logical_id, res["Type"], value


def test_every_stack_is_suffixed(dev_templates):
    assert sorted(dev_templates) == sorted(f"{name}-dev" for name in STACKS)


def test_physical_names_carry_the_suffix(dev_templates):
    # Shared on purpose: the bridge writes /openclaw/container under a fixed name.
    shared = {"/openclaw/container"}
    for stack, logical_id, _type, value in _physical_names(dev_templates):
        if value in shared:
            continue
        assert value.endswith(("-dev", "_dev")) or "-dev-" in value, f"{stack}/{logical_id}: {value}"


def test_suffixed_environment_reuses_shared_invocation_logging(dev_templates):
    resources = dev_templates["OpenClawObservability-dev"]["Resources"].values()
    assert not [r for r in resources if r["Type"] == "AWS::Logs::LogGroup"]
    assert not [r for r in resources if r["Type"] == "Custom::AWS"
                and "PutModelInvocationLoggingConfiguration" in json.dumps(r)]


def test_suffixed_environment_can_manage_invocation_logging(tmp_path):
    templates = _synth(tmp_path, environment_suffix="dev", manage_bedrock_invocation_logging="true")
    resources = templates["OpenClawObservability-dev"]["Resources"].values()
    assert [r for r in resources if r["Type"] == "AWS::Logs::LogGroup"
            and r["Properties"]["LogGroupName"] == "/aws/bedrock/invocation-logs"]
    assert [r for r in resources if r["Type"] == "Custom::AWS"
            and "PutModelInvocationLoggingConfiguration" in json.dumps(r)]
