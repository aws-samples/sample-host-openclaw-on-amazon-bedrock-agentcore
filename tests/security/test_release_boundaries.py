from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda"))

from workspace_broker.index import build_workspace_session_policy  # noqa: E402


_HIGH_CONFIDENCE_CREDENTIAL_PATTERNS = {
    "aws_access_key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "openai_project_key": re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "telegram_bot_token": re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,}"),
}


def _scan_production_credentials(root: Path) -> list[tuple[str, str, int]]:
    candidates = [
        path for path in (root / "app.py", root / "cdk.json") if path.is_file()
    ]
    for directory in ("stacks", "lambda", "bridge", "web", "scripts"):
        path = root / directory
        if path.is_dir():
            candidates.extend(item for item in path.rglob("*") if item.is_file())
    findings: list[tuple[str, str, int]] = []
    for path in sorted(set(candidates)):
        relative = path.relative_to(root)
        if (
            "node_modules" in relative.parts
            or path.name.startswith("test_")
            or ".test." in path.name
            or path.suffix
            not in {".py", ".js", ".jsx", ".json", ".html", ".css", ".sh", ""}
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(source.splitlines(), 1):
            for label, pattern in _HIGH_CONFIDENCE_CREDENTIAL_PATTERNS.items():
                if pattern.search(line):
                    findings.append((label, relative.as_posix(), line_number))
    return findings


def _node_json(source: str) -> object:
    completed = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_runtime_has_exact_curated_surface_loopback_gateway_and_no_provider_secrets() -> None:
    result = _node_json(
        r"""
        const policy = require('./bridge/runtime-policy');
        const poison = {
          AWS_REGION: 'eu-west-1',
          AWS_DEFAULT_REGION: 'eu-west-1',
          AWS_CONFIG_FILE: '/run/scoped/config',
          AWS_SDK_LOAD_CONFIG: '1',
          AWS_EC2_METADATA_DISABLED: 'true',
          AWS_SHARED_CREDENTIALS_FILE: '/dev/null',
          S3_USER_FILES_BUCKET: 'personal-operator-workspace',
          PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE: '/run/scoped/creds.json',
          TELEGRAM_BOT_TOKEN: 'must-not-cross',
          GMAIL_REFRESH_TOKEN: 'must-not-cross',
          GOOGLE_CLIENT_SECRET: 'must-not-cross',
          OPENAI_API_KEY: 'must-not-cross',
          APPROVAL_SIGNING_SECRET: 'must-not-cross',
          CONTROL_TABLE_NAME: 'must-not-cross',
          AWS_ACCESS_KEY_ID: 'must-not-cross',
          AWS_SECRET_ACCESS_KEY: 'must-not-cross',
          AWS_SESSION_TOKEN: 'must-not-cross',
        };
        const config = policy.buildOpenClawConfig({gatewayToken: 'x'.repeat(43)});
        const child = policy.buildOpenClawChildEnv({
          scopedEnv: poison,
          workspacePrefix: 'pilot_alpha',
        });
        process.stdout.write(JSON.stringify({
          approved: policy.APPROVED_TOOLS,
          runtimePolicy: policy.buildRuntimePolicy(),
          models: config.models,
          agentModel: config.agents.defaults.model,
          agentModels: config.agents.defaults.models,
          gateway: config.gateway,
          commands: config.commands,
          channels: config.channels,
          skills: config.skills,
          agentSkills: config.agents.defaults.skills,
          child,
        }));
        """
    )

    assert result["approved"] == [
        "po_file_list",
        "po_file_read",
        "po_file_write",
        "po_file_delete",
        "po_web_read",
        "po_schedule_list",
        "po_schedule_propose",
        "po_schedule_cancel_propose",
        "po_compute_run",
        "po_compute_status",
    ]
    assert result["runtimePolicy"] == {
        "tools": {
            "profile": "minimal",
            "alsoAllow": [
                "po_file_list",
                "po_file_read",
                "po_file_write",
                "po_file_delete",
                "po_web_read",
                "po_schedule_list",
                "po_schedule_propose",
                "po_schedule_cancel_propose",
                "po_compute_run",
                "po_compute_status",
            ],
            "deny": ["session_status"],
        },
        "plugins": {
            "enabled": True,
            "allow": ["personal-operator"],
            "load": {"paths": ["/app/plugins/personal-operator"]},
            "entries": {"personal-operator": {"enabled": True}},
            "slots": {"memory": "none"},
        },
    }
    assert result["models"] == {
        "mode": "replace",
        "providers": {
            "agentcore": {
                "baseUrl": "http://127.0.0.1:18790/v1",
                "apiKey": "local",
                "api": "openai-completions",
                "models": [
                    {"id": "bedrock-agentcore", "name": "Bedrock AgentCore"}
                ],
            }
        },
    }
    assert result["agentModel"] == {"primary": "agentcore/bedrock-agentcore"}
    assert result["agentModels"] == {"agentcore/bedrock-agentcore": {}}
    assert result["gateway"]["mode"] == "local"
    assert result["gateway"]["trustedProxies"] == ["127.0.0.1"]
    assert result["gateway"]["controlUi"] == {"enabled": False}
    assert result["commands"] == {"text": False}
    assert result["channels"] == {}
    assert result["skills"] == {"allowBundled": []}
    assert result["agentSkills"] == []
    child = result["child"]
    assert child["PERSONAL_OPERATOR_WORKSPACE_PREFIX"] == "pilot_alpha"
    assert child["AWS_EC2_METADATA_DISABLED"] == "true"
    assert child["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    forbidden = {
        "TELEGRAM_BOT_TOKEN",
        "GMAIL_REFRESH_TOKEN",
        "GOOGLE_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "APPROVAL_SIGNING_SECRET",
        "CONTROL_TABLE_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    assert forbidden.isdisjoint(child)
    assert "must-not-cross" not in json.dumps(child)


def test_broker_session_policy_cartesian_canary_is_exactly_user_prefix_scoped() -> None:
    users = ["pilot_alpha", "pilot_bravo", "pilot_charlie"]
    policies = {
        user: json.loads(
            build_workspace_session_policy(
                bucket="personal-operator-workspace",
                namespace=user,
                account="123456789012",
                cmk_arn=(
                    "arn:aws:kms:eu-west-1:123456789012:key/"
                    "11111111-2222-3333-4444-555555555555"
                ),
            )
        )
        for user in users
    }

    for user in users:
        statements = policies[user]["Statement"]
        objects, listing, kms = statements
        assert objects == {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": f"arn:aws:s3:::personal-operator-workspace/{user}/*",
        }
        assert listing == {
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::personal-operator-workspace",
            "Condition": {"StringLike": {"s3:prefix": f"{user}/*"}},
        }
        assert kms["Resource"].endswith(
            ":key/11111111-2222-3333-4444-555555555555"
        )
        rendered = json.dumps(statements)
        for other in users:
            if other != user:
                assert other not in rendered
        actions = {
            action
            for statement in statements
            for action in (
                [statement["Action"]]
                if isinstance(statement["Action"], str)
                else statement["Action"]
            )
        }
        assert actions == {
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:GenerateDataKey",
        }


def test_runtime_cannot_assume_workspace_role_or_construct_session_policy() -> None:
    source = (ROOT / "bridge/scoped-credentials.js").read_text(encoding="utf-8")
    package = json.loads((ROOT / "bridge/package.json").read_text(encoding="utf-8"))

    assert "buildSessionPolicy" not in source
    assert "AssumeRole" not in source
    assert "@aws-sdk/client-sts" not in package["dependencies"]
    assert "@aws-sdk/client-lambda" in package["dependencies"]
    assert "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME" in source


def test_web_and_retention_plane_has_an_explicit_no_mint_runtime_driver() -> None:
    composition = (ROOT / "lambda/web/composition.py").read_text(encoding="utf-8")
    web_stack = (ROOT / "stacks/web_stack.py").read_text(encoding="utf-8")

    assert "workspace_capability_signer=NoWorkspaceCapabilitySigner()" in composition
    assert "WORKSPACE_CAPABILITY_SECRET_ID" not in composition
    assert "WORKSPACE_CAPABILITY_SECRET_ID" not in web_stack


def test_runtime_image_and_source_metadata_are_immutable_at_local_boundary() -> None:
    context = json.loads((ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")
    upstream = (ROOT / "docs/UPSTREAM.md").read_text(encoding="utf-8")

    assert context["region"] == "eu-west-1"
    assert context["image_version"].isdecimal()
    assert "ARG OPENCLAW_VERSION=2026.7.2" in dockerfile
    assert (
        "ARG OPENCLAW_SOURCE_COMMIT=4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"
        in dockerfile
    )
    assert 'test "$(git rev-parse HEAD)" = "$OPENCLAW_SOURCE_COMMIT"' in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "node:latest" not in dockerfile
    node_base = (
        "public.ecr.aws/docker/library/node:24.15.0-slim@sha256:"
        "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
    )
    assert dockerfile.count(node_base) == 2
    assert "immutable ECR digest" in upstream


@pytest.mark.parametrize(
    "relative",
    [
        "bridge/agentcore-contract.js",
        "bridge/runtime-policy.js",
        "bridge/entrypoint.sh",
        "bridge/Dockerfile",
    ],
)
def test_runtime_launch_sources_contain_no_product_provider_secret_contracts(
    relative: str,
) -> None:
    source = (ROOT / relative).read_text(encoding="utf-8")
    for forbidden in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_SECRET_ID",
        "GMAIL_REFRESH_TOKEN",
        "GOOGLE_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "APPROVAL_SIGNING_SECRET",
        "CONTROL_TABLE_NAME",
    ):
        assert forbidden not in source


def test_no_deployed_function_url_or_public_openclaw_gateway_is_declared() -> None:
    stack_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "stacks").glob("*.py"))
    )
    web_stack = (ROOT / "stacks/web_stack.py").read_text(encoding="utf-8")
    router_stack = (ROOT / "stacks/router_stack.py").read_text(encoding="utf-8")

    assert "FunctionUrl" not in stack_sources
    assert ".add_function_url(" not in stack_sources
    assert "default_integration=" not in web_stack
    assert "default_integration=" not in router_stack
    assert "origins.HttpOrigin(runtime_arn" not in web_stack
    assert "agentcore" not in " ".join(
        line.strip().casefold()
        for line in web_stack.splitlines()
        if "add_routes(" in line or "route_path" in line
    )


def test_forbidden_runtime_capability_packages_and_trees_are_absent() -> None:
    package = json.loads((ROOT / "bridge/package.json").read_text(encoding="utf-8"))
    dependencies = set(package["dependencies"])
    assert {
        "playwright",
        "playwright-core",
        "@aws-sdk/client-scheduler",
        "@aws-sdk/client-secrets-manager",
    }.isdisjoint(dependencies)
    assert not (ROOT / "bridge/skills").exists()
    assert not (ROOT / "bridge/plugins/clawhub").exists()
    assert sorted(
        path.name for path in (ROOT / "bridge/plugins").iterdir() if path.is_dir()
    ) == ["personal-operator"]


def test_production_sources_contain_no_high_confidence_literal_credentials() -> None:
    assert _scan_production_credentials(ROOT) == []


def test_browser_release_inputs_are_inside_the_literal_credential_scan(
    tmp_path: Path,
) -> None:
    source = tmp_path / "web" / "src" / "App.jsx"
    source.parent.mkdir(parents=True)
    source.write_text(
        'export const accidentalBrowserSecret = "AKIAABCDEFGHIJKLMNOP";\n',
        encoding="utf-8",
    )

    assert _scan_production_credentials(tmp_path) == [
        ("aws_access_key", "web/src/App.jsx", 1)
    ]
