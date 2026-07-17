"""Static contract tests for the frozen Personal Operator runtime defaults."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_cdk_product_defaults_are_frozen() -> None:
    context = _json("cdk.json")["context"]

    assert context["region"] == "eu-west-1"
    assert context["default_model_id"] == "eu.anthropic.claude-sonnet-4-6"
    assert context["session_idle_timeout"] == 1800
    assert context["session_max_lifetime"] == 28800
    assert context["workspace_sync_interval_seconds"] == 300
    assert context["user_files_ttl_days"] == 30
    assert context["registration_open"] is False
    assert context["enable_browser"] is False
    assert context["runtime_id"] == ""
    assert context["runtime_endpoint_id"] == ""
    assert context["image_version"] == "71"


def test_bridge_runtime_versions_are_frozen() -> None:
    package = _json("bridge/package.json")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert package["engines"]["node"] == ">=24.15.0"
    assert re.findall(r"^FROM .*node:(\S+)", dockerfile, flags=re.MULTILINE) == [
        "24.15.0-slim",
        "24.15.0-slim",
    ]
    assert re.findall(
        r"^ARG OPENCLAW_VERSION=([^\s]+)", dockerfile, flags=re.MULTILINE
    ) == ["2026.7.2"]
    assert re.findall(
        r"^ARG OPENCLAW_SOURCE_COMMIT=([0-9a-f]+)", dockerfile, flags=re.MULTILINE
    ) == ["4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"]
    assert 'git fetch --depth 1 origin "$OPENCLAW_SOURCE_COMMIT"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "$OPENCLAW_SOURCE_COMMIT"' in dockerfile
    assert (
        "test \"$(node -p 'require(\"./package.json\").version')\" "
        '= "$OPENCLAW_VERSION"'
    ) in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "COPY --from=builder /opt/openclaw /opt/openclaw" in dockerfile
    assert "ln -s /opt/openclaw/openclaw.mjs /usr/local/bin/openclaw" in dockerfile
    assert "npm install -g openclaw@" not in dockerfile


def test_bridge_image_copies_only_the_reviewed_plugin() -> None:
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "clawhub" not in dockerfile.casefold()
    assert "COPY plugins/personal-operator /app/plugins/personal-operator" in dockerfile
    assert "COPY skills/" not in dockerfile
    assert "/skills/" not in dockerfile
    assert "ln -s /opt/openclaw /app/node_modules/openclaw" in dockerfile
    assert "COPY gateway-invocation.js /app/gateway-invocation.js" in dockerfile


def test_legacy_executable_skill_trees_are_absent() -> None:
    assert not (ROOT / "bridge/skills").exists()
    assert not (ROOT / "bridge/agentcore-browser.test.js").exists()
    assert not (ROOT / "bridge/browser-lifecycle.test.js").exists()


def test_personal_operator_plugin_package_is_frozen() -> None:
    package = _json("bridge/plugins/personal-operator/package.json")
    manifest = _json("bridge/plugins/personal-operator/openclaw.plugin.json")

    expected_tools = [
        "po_file_list",
        "po_file_read",
        "po_file_write",
        "po_file_delete",
    ]
    assert package["type"] == "module"
    assert package["openclaw"]["extensions"] == ["./index.js"]
    assert package["openclaw"]["compat"]["pluginApi"] == ">=2026.7.2"
    assert manifest["id"] == "personal-operator"
    assert manifest["contracts"]["tools"] == expected_tools
    assert manifest["activation"]["onStartup"] is True


def test_contract_has_no_channel_delivery_or_executable_tool_runtime() -> None:
    contract = (ROOT / "bridge/agentcore-contract.js").read_text(encoding="utf-8")
    lightweight = (ROOT / "bridge/lightweight-agent.js").read_text(encoding="utf-8")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "TELEGRAM_BOT_TOKEN" not in contract
    assert "api.telegram.org" not in contract
    assert "TELEGRAM_CHANNEL_SECRET_ID" not in contract
    assert "createTelegramStreamer" not in contract
    assert "eventbridge-cron" not in contract.casefold()
    assert "clawhub" not in contract.casefold()
    assert "api-keys" not in contract.casefold()
    assert "agentcore-browser" not in contract.casefold()
    assert 'require("./runtime-policy")' in contract
    assert "buildOpenClawConfig" in contract
    assert "createLocalGatewayToken" in contract
    assert "GATEWAY_TOKEN_SECRET_ID" not in contract
    assert "child_process" not in lightweight
    assert "/skills/" not in lightweight
    assert "playwright-core" not in _json("bridge/package.json")["dependencies"]
    assert "@aws-sdk/client-scheduler" not in _json("bridge/package.json")["dependencies"]
    assert "playwright-core" not in dockerfile


def test_bridge_dependency_install_is_locked() -> None:
    package = _json("bridge/package.json")
    lock_path = ROOT / "bridge/package-lock.json"
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert lock_path.is_file(), "bridge/package-lock.json must be committed"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"][""]["engines"] == package["engines"]
    assert "COPY package.json package-lock.json /app/" in dockerfile
    assert "npm ci --omit=dev" in dockerfile
    assert "npm init" not in dockerfile
    assert "npm install --omit=dev" not in dockerfile


def test_readme_records_the_enforced_runtime_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Runtime boundary now enforced" in readme
    assert "`po_file_list`" in readme
    assert "`po_file_delete`" in readme
    assert "arbitrary shell execution is disabled" in readme
    assert "Telegram delivery remains outside the runtime" in readme
    assert "ordinary slash-prefixed input is model text" in readme
    assert "exact `/new` and `/reset`" in readme
    assert "Text commands are disabled" not in readme


def test_cdk_nag_guard_rejects_missing_reports(tmp_path: Path) -> None:
    script = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    match = re.search(
        r"check_cdk_nag\(\) \{\n\s+\"\$PYTHON\" - \"\$SYNTH_OUT\" <<'PY'\n"
        r"(?P<checker>.*?)\nPY\n\}",
        script,
        flags=re.DOTALL,
    )
    assert match is not None, "could not locate the embedded cdk-nag checker"

    completed = subprocess.run(
        [sys.executable, "-c", match.group("checker"), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "No AwsSolutions NagReport CSV files found" in completed.stderr


def test_runtime_role_is_separated_from_workspace_and_legacy_cron_authority() -> None:
    agentcore = (ROOT / "stacks/agentcore_stack.py").read_text(encoding="utf-8")
    cron = (ROOT / "stacks/cron_stack.py").read_text(encoding="utf-8")

    assert "WorkspaceSessionRole" in agentcore
    assert "WorkspaceSessionRoleArn" in agentcore
    assert "workspace_session_role" in agentcore
    assert "self.user_files_bucket.grant_read_write(self.execution_role)" not in agentcore
    assert 'actions=["sts:AssumeRole"]' in agentcore
    assert "scheduler:" not in agentcore.casefold()
    assert "dynamodb:" not in agentcore.casefold()
    assert "iam:PassRole" not in agentcore
    assert "DIRECT_CRON_DISABLED" in cron
    assert "aws_scheduler" not in cron


def test_workspace_role_policy_is_exact_s3_and_cmk_only() -> None:
    source = (ROOT / "stacks/agentcore_stack.py").read_text(encoding="utf-8")

    required_actions = {
        '"s3:ListBucket"',
        '"s3:GetObject"',
        '"s3:PutObject"',
        '"s3:DeleteObject"',
        '"kms:Encrypt"',
        '"kms:Decrypt"',
        '"kms:GenerateDataKey"',
    }
    for action in required_actions:
        assert action in source
    assert '"kms:ViaService": "s3.eu-west-1.amazonaws.com"' in source
    assert '"kms:CallerAccount": account' in source


def test_deploy_and_e2e_contract_use_exact_region_and_workspace_role() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    local = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    e2e = (ROOT / "scripts/e2e-deploy-and-test.sh").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'REQUIRED_REGION="eu-west-1"' in deploy
    assert 'WORKSPACE_SESSION_ROLE_ARN=$(aws cloudformation describe-stacks' in deploy
    assert '--env "WORKSPACE_SESSION_ROLE_ARN=$WORKSPACE_SESSION_ROLE_ARN"' in deploy
    assert "EVENTBRIDGE_SCHEDULE_GROUP" not in deploy
    assert "CRON_LAMBDA_ARN" not in deploy
    assert "EVENTBRIDGE_ROLE_ARN" not in deploy
    assert 'AWS_TEST_REGION="eu-west-1"' in local
    assert 'REQUIRED_REGION="eu-west-1"' in e2e
    assert 'CronStack(app, "OpenClawCron", env=env)' in app
    assert 'REQUIRED_REGION = "eu-west-1"' in app


def test_e2e_region_resolver_rejects_explicit_wrong_region_before_clients(monkeypatch) -> None:
    from tests.e2e import config

    monkeypatch.setenv("CDK_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(
        config.boto3,
        "client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("AWS client must not be created")
        ),
    )

    try:
        config.load_config()
    except RuntimeError as error:
        assert "eu-west-1" in str(error)
    else:
        raise AssertionError("wrong region was accepted")


def _synth_agentcore_template(region: str = "eu-west-1") -> dict:
    from aws_cdk import App, Environment, Stack, aws_ec2 as ec2
    from aws_cdk.assertions import Template

    from stacks.agentcore_stack import AgentCoreStack

    account = "123456789012"
    app = App(
        context={
            "runtime_id": "runtime-test",
            "runtime_endpoint_id": "endpoint-test",
            "user_files_ttl_days": "30",
            "enable_browser": "false",
        }
    )
    env = Environment(account=account, region=region)
    network = Stack(app, f"Network{region.replace('-', '')}", env=env)
    vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=0)
    stack = AgentCoreStack(
        app,
        f"AgentCore{region.replace('-', '')}",
        cmk_arn=f"arn:aws:kms:{region}:{account}:key/test-key",
        vpc=vpc,
        private_subnet_ids=["subnet-00000000000000001"],
        env=env,
    )
    return Template.from_stack(stack).to_json()


def _role_and_statements(template: dict, role_name: str) -> tuple[dict, list[dict]]:
    resources = template["Resources"]
    role_id, role = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role"
        and resource["Properties"].get("RoleName") == role_name
    )
    statements = []
    for resource in resources.values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        roles = resource["Properties"].get("Roles", [])
        if {"Ref": role_id} in roles:
            statements.extend(
                resource["Properties"]["PolicyDocument"].get("Statement", [])
            )
    return role, statements


def _flatten_statement_actions(statements: list[dict]) -> list[str]:
    actions = []
    for statement in statements:
        value = statement.get("Action", [])
        actions.extend(value if isinstance(value, list) else [value])
    return actions


def test_synthesized_workspace_role_has_exact_trust_and_base_authority() -> None:
    template = _synth_agentcore_template()
    role, statements = _role_and_statements(
        template, "openclaw-workspace-session-role-eu-west-1"
    )

    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    assert trust[0]["Action"] == "sts:AssumeRole"
    assert trust[0]["Principal"] == {
        "AWS": {
            "Fn::Join": [
                "",
                [
                    "arn:",
                    {"Ref": "AWS::Partition"},
                    ":iam::123456789012:root",
                ],
            ]
        }
    }
    assert trust[0]["Condition"] == {
        "ArnEquals": {
            "aws:PrincipalArn": (
                "arn:aws:iam::123456789012:role/"
                "openclaw-agentcore-execution-role-eu-west-1"
            )
        },
        "StringLike": {"sts:RoleSessionName": "workspace-*"},
    }

    assert set(_flatten_statement_actions(statements)) == {
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey",
    }
    assert all(statement.get("Resource") != "*" for statement in statements)
    list_statement = next(
        statement for statement in statements if statement["Action"] == "s3:ListBucket"
    )
    object_statement = next(
        statement
        for statement in statements
        if isinstance(statement["Action"], list)
        and "s3:GetObject" in statement["Action"]
    )
    assert list_statement["Resource"] == {
        "Fn::GetAtt": ["UserFilesBucketCFDFD8C0", "Arn"]
    }
    assert "Condition" not in list_statement
    assert object_statement["Resource"] == {
        "Fn::Join": [
            "",
            [
                {"Fn::GetAtt": ["UserFilesBucketCFDFD8C0", "Arn"]},
                "/*",
            ],
        ]
    }
    kms_statement = next(
        statement
        for statement in statements
        if "kms:Encrypt" in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert kms_statement["Resource"] == (
        "arn:aws:kms:eu-west-1:123456789012:key/test-key"
    )
    assert kms_statement["Condition"] == {
        "StringEquals": {
            "kms:ViaService": "s3.eu-west-1.amazonaws.com",
            "kms:CallerAccount": "123456789012",
        }
    }


def test_synthesized_execution_role_has_only_exact_workspace_assume_authority() -> None:
    template = _synth_agentcore_template()
    _, statements = _role_and_statements(
        template, "openclaw-agentcore-execution-role-eu-west-1"
    )
    actions = _flatten_statement_actions(statements)
    forbidden_prefixes = ("s3:", "scheduler:", "events:", "dynamodb:")

    assert not any(action.startswith(forbidden_prefixes) for action in actions)
    assert not any(action.startswith("secretsmanager:") for action in actions)
    assert not any(action.startswith("iam:") for action in actions)
    assert "iam:PassRole" not in actions
    assert [action for action in actions if action.startswith("sts:")] == [
        "sts:AssumeRole"
    ]
    assume_statements = [
        statement
        for statement in statements
        if statement.get("Action") == "sts:AssumeRole"
    ]
    assert assume_statements == [
        {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Resource": (
                "arn:aws:iam::123456789012:role/"
                "openclaw-workspace-session-role-eu-west-1"
            ),
        }
    ]


def test_agentcore_stack_rejects_wrong_region() -> None:
    try:
        _synth_agentcore_template("us-west-2")
    except ValueError as error:
        assert "eu-west-1" in str(error)
    else:
        raise AssertionError("AgentCoreStack accepted a non-canonical region")


def test_synthesized_cron_tombstone_has_no_runtime_resources() -> None:
    from aws_cdk import App, Environment
    from aws_cdk.assertions import Template

    from stacks.cron_stack import CronStack

    app = App()
    stack = CronStack(
        app,
        "OpenClawCron",
        env=Environment(account="123456789012", region="eu-west-1"),
    )
    template = Template.from_stack(stack).to_json()

    assert template.get("Resources", {}) == {}
    assert template["Outputs"]["DirectCronStatus"]["Value"] == (
        "DIRECT_CRON_DISABLED"
    )


def test_shell_region_guards_fail_before_aws_cli(tmp_path: Path) -> None:
    poison_bin = tmp_path / "bin"
    poison_bin.mkdir()
    marker = tmp_path / "aws-called"
    aws = poison_bin / "aws"
    aws.write_text(
        '#!/usr/bin/env bash\ntouch "$AWS_POISON_MARKER"\nexit 97\n',
        encoding="utf-8",
    )
    aws.chmod(0o755)

    for relative_script in ("scripts/deploy.sh", "scripts/e2e-deploy-and-test.sh"):
        env = os.environ.copy()
        env.pop("CDK_DEFAULT_REGION", None)
        env.pop("AWS_DEFAULT_REGION", None)
        env.update(
            {
                "AWS_REGION": "us-west-2",
                "AWS_POISON_MARKER": str(marker),
                "PATH": f"{poison_bin}:{env['PATH']}",
            }
        )
        completed = subprocess.run(
            ["bash", str(ROOT / relative_script)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 1
        assert "must be exactly eu-west-1" in completed.stderr
        assert not marker.exists(), f"{relative_script} called aws before region gate"


def test_unused_cognito_and_gateway_infrastructure_is_absent() -> None:
    source = (ROOT / "stacks/security_stack.py").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "aws_cognito" not in source
    assert "GatewayTokenSecret" not in source
    assert "gateway_token_secret" not in source
    assert "CognitoPasswordSecret" not in source
    assert "cognito_password_secret" not in source
    assert 'channel_names = ["telegram", "slack", "feishu"]' in source
    assert "GATEWAY_TOKEN_SECRET_ID" not in deploy
    assert "COGNITO_USER_POOL_ID" not in deploy
    assert "COGNITO_CLIENT_ID" not in deploy
    assert "COGNITO_PASSWORD_SECRET_ID" not in deploy


def test_synthesized_security_stack_has_only_active_control_plane_secrets() -> None:
    from aws_cdk import App, Environment
    from aws_cdk.assertions import Template

    from stacks.security_stack import SecurityStack

    app = App(context={"enable_cloudtrail": False})
    stack = SecurityStack(
        app,
        "Security",
        env=Environment(account="123456789012", region="eu-west-1"),
    )
    template = Template.from_stack(stack).to_json()
    resources = template.get("Resources", {}).values()

    assert not any(resource["Type"].startswith("AWS::Cognito::") for resource in resources)
    secret_names = {
        resource["Properties"]["Name"]
        for resource in resources
        if resource["Type"] == "AWS::SecretsManager::Secret"
    }
    assert secret_names == {
        "openclaw/channels/telegram",
        "openclaw/channels/slack",
        "openclaw/channels/feishu",
        "openclaw/webhook-secret",
    }
