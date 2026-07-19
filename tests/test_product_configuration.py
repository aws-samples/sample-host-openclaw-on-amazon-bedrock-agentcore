"""Static contract tests for the frozen Personal Operator runtime defaults."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from release_tools.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _without_aws_regions() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("CDK_DEFAULT_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        env.pop(name, None)
    return env


def _create_e2e_script_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Copy the real E2E script into a fake project with inert command shims."""
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    e2e_script = scripts / "e2e-deploy-and-test.sh"
    e2e_script.write_text(
        (ROOT / "scripts/e2e-deploy-and-test.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    e2e_script.chmod(0o755)
    (project / "cdk.json").write_text(
        json.dumps(
            {
                "context": {
                    "region": "eu-west-1",
                    "default_model_id": "eu.anthropic.claude-sonnet-4-6",
                    "image_version": "71",
                }
            }
        ),
        encoding="utf-8",
    )
    build = project / "build"
    build.mkdir()
    image = (
        "123456789012.dkr.ecr.eu-west-1.amazonaws.com/"
        "personal-operator/bridge@sha256:" + "a" * 64
    )
    role_arn = (
        "arn:aws:iam::123456789012:role/"
        "openclaw-agentcore-execution-role-eu-west-1"
    )
    runtime_configuration = {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": image}
        },
        "authorizerConfiguration": {},
        "environmentVariables": {
            "AWS_DEFAULT_REGION": "eu-west-1",
            "AWS_REGION": "eu-west-1",
            "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
            "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
            "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                "workspace-credential-broker"
            ),
            "WORKSPACE_SYNC_INTERVAL_MS": "300000",
        },
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 28800,
        },
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": ["sg-00000000000000001"],
                "subnets": [
                    "subnet-00000000000000001",
                    "subnet-00000000000000002",
                ],
            },
        },
        "metadataConfiguration": {"requireMMDSV2": True},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "requestHeaderConfiguration": {},
    }
    (build / "runtime-context.json").write_text(
        json.dumps(
            {
                "schema": "personal-operator.runtime-context.v3",
                "sourceCommit": "a" * 40,
                "account": "123456789012",
                "region": "eu-west-1",
                "runtimeId": "openclaw_agent-0123456789",
                "runtimeEndpointId": "release_endpoint-0123456789",
                "runtimeEndpointName": "release_" + "a" * 40,
                "runtimeVersion": "1",
                "runtimeArn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
                    "agent/12345678-1234-1234-1234-123456789abc:1"
                ),
                "runtimeImageUri": image,
                "executionRoleArn": role_arn,
                "runtimeConfiguration": runtime_configuration,
                "runtimeConfigurationSha256": hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "executionRoleArn": role_arn,
                            "runtimeConfiguration": runtime_configuration,
                        }
                    )
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    call_log = tmp_path / "calls.log"
    deploy = scripts / "deploy.sh"
    _write_executable(
        deploy,
        '#!/usr/bin/env bash\nprintf "DEPLOY <%s>\\n" "$*" >> "$E2E_CALL_LOG"\n',
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "aws",
        r'''#!/usr/bin/env bash
printf 'AWS <%s>\n' "$*" >> "$E2E_CALL_LOG"
args="$*"
case "$args" in
  "sts get-caller-identity"*) echo '123456789012' ;;
  *"WorkspaceSessionRoleArn"*) echo 'arn:aws:iam::123456789012:role/openclaw-workspace-session-role-eu-west-1' ;;
  *"ExecutionRoleArn"*) echo 'arn:aws:iam::123456789012:role/openclaw-agentcore-execution-role-eu-west-1' ;;
  *"UserFilesBucketName"*) echo 'openclaw-user-files-test' ;;
  *"SecretsCmk"*) echo 'arn:aws:kms:eu-west-1:123456789012:key/test-key' ;;
  *"get-agent-runtime"*"agentRuntimeArn"*) echo "${E2E_FAKE_RUNTIME_ARN:-arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/12345678-1234-1234-1234-123456789abc:1}" ;;
  *"get-agent-runtime"*"agentRuntimeId"*) echo 'openclaw_agent-0123456789' ;;
  *"get-agent-runtime"*"status"*) echo 'READY' ;;
  *"get-agent-runtime"*"roleArn"*) echo "${E2E_FAKE_RUNTIME_ROLE:-arn:aws:iam::123456789012:role/openclaw-agentcore-execution-role-eu-west-1}" ;;
  *"get-agent-runtime"*"WORKSPACE_SESSION_ROLE_ARN"*) echo 'arn:aws:iam::123456789012:role/openclaw-workspace-session-role-eu-west-1' ;;
  *"get-agent-runtime"*"AWS_REGION"*) echo 'eu-west-1' ;;
  *"get-agent-runtime"*"BEDROCK_MODEL_ID"*) echo 'eu.anthropic.claude-sonnet-4-6' ;;
  *"get-agent-runtime"*"S3_USER_FILES_BUCKET"*) echo 'openclaw-user-files-test' ;;
  *"get-agent-runtime"*"CMK_ARN"*) echo 'arn:aws:kms:eu-west-1:123456789012:key/test-key' ;;
  *"list-agent-runtime-endpoints"*) echo 'release_endpoint-0123456789' ;;
  *"lambda get-function-configuration"*"AGENTCORE_RUNTIME_ARN"*) echo "${E2E_FAKE_ROUTER_RUNTIME_ARN:-arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/12345678-1234-1234-1234-123456789abc:1}" ;;
  *"lambda get-function-configuration"*"AGENTCORE_QUALIFIER"*) echo 'release_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  *"lambda get-function-configuration"*"Role"*) echo 'arn:aws:iam::123456789012:role/OpenClawRouter-RouterFnServiceRole-test' ;;
  *"iam list-role-policies"*) echo 'OpenClawRouter-RouterFnServiceRoleDefaultPolicy-test' ;;
  *"iam get-role-policy"*) printf '%s\n' "${E2E_FAKE_ROUTER_IAM_RESOURCES:-arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/openclaw_agent-0123456789 arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/openclaw_agent-0123456789/runtime-endpoint/release_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" ;;
  *"describe-repositories"*) echo 'example.invalid/openclaw-bridge' ;;
  *"get-login-password"*) echo 'not-a-password' ;;
  *"get-secret-value"*) echo 'not-a-token' ;;
  *) echo "unexpected fake aws call: $args" >&2; exit 98 ;;
esac
''',
    )
    for name in ("cdk", "docker", "curl", "sleep"):
        _write_executable(
            fake_bin / name,
            f'#!/usr/bin/env bash\nprintf "{name.upper()} <%s>\\n" "$*" >> "$E2E_CALL_LOG"\n',
        )

    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text(
        f'export PATH="{venv_bin}:$PATH"\nhash -r\n', encoding="utf-8"
    )
    python_shim = r'''#!/usr/bin/env bash
printf 'PYTHON' >> "$E2E_CALL_LOG"
for arg in "$@"; do printf ' <%s>' "$arg" >> "$E2E_CALL_LOG"; done
printf '\n' >> "$E2E_CALL_LOG"
case "$*" in
  *"image_version"*) echo '71' ;;
  *"json.dumps"*) echo '"notification"' ;;
  *) cat >/dev/null || true ;;
esac
'''
    _write_executable(venv_bin / "python", python_shim)
    _write_executable(venv_bin / "python3", python_shim)

    env = _without_aws_regions()
    env.update(
        {
            "E2E_CALL_LOG": str(call_log),
            "E2E_TELEGRAM_CHAT_ID": "10002",
            "E2E_TELEGRAM_USER_ID": "10002",
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return e2e_script, env, call_log


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
    assert context["runtime_source_commit"] == ""
    assert context["runtime_id"] == ""
    assert context["runtime_endpoint_id"] == ""
    assert context["runtime_endpoint_name"] == ""
    assert context["runtime_version"] == ""
    assert context["runtime_arn"] == ""
    assert context["runtime_image_uri"] == ""
    assert context["image_version"] == "71"


def test_bridge_runtime_versions_are_frozen() -> None:
    package = _json("bridge/package.json")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert package["engines"]["node"] == ">=24.15.0"
    pinned_node = (
        "24.15.0-slim@sha256:"
        "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
    )
    assert re.findall(r"^FROM .*node:(\S+)", dockerfile, flags=re.MULTILINE) == [
        pinned_node,
        pinned_node,
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


def test_bridge_image_contains_only_exact_reviewed_capability_artifacts() -> None:
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")
    canonical_root = ROOT / "specs/capabilities"
    image_root = ROOT / "bridge/capabilities"

    expected = sorted(
        path.relative_to(canonical_root)
        for path in canonical_root.rglob("*")
        if path.is_file()
    )
    actual = (
        sorted(
            path.relative_to(image_root)
            for path in image_root.rglob("*")
            if path.is_file()
        )
        if image_root.is_dir()
        else []
    )

    assert actual == expected
    for relative in expected:
        assert (image_root / relative).read_bytes() == (
            canonical_root / relative
        ).read_bytes()

    assert "COPY capability-catalog.js /app/capability-catalog.js" in dockerfile
    assert (
        "COPY capabilities/catalog-v1.json /app/capabilities/catalog-v1.json"
        in dockerfile
    )
    assert "COPY capabilities/schemas /app/capabilities/schemas" in dockerfile


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
        "po_web_read",
        "po_schedule_list",
        "po_schedule_propose",
        "po_schedule_cancel_propose",
        "po_compute_run",
        "po_compute_status",
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


def test_local_aggregate_executes_every_phase5_python_and_stack_suite() -> None:
    script = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    required = {
        "lambda/compute",
        "lambda/connectors",
        "lambda/browser",
        "lambda/scheduler",
        "tests/test_compute_stack.py",
        "tests/test_browser_stack.py",
        "tests/test_scheduler_stack.py",
    }
    for suite in required:
        assert suite in script, f"local aggregate omits {suite}"


def test_local_gate_inventory_includes_portable_v2_unit_suite() -> None:
    script = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    python_units = script.split(
        'run_check "Python unit tests"', 1
    )[1].split('run_check "E2E session-control unit tests"', 1)[0]

    assert "lambda/portable" in python_units


def test_runtime_role_is_separated_from_workspace_and_legacy_cron_authority() -> None:
    agentcore = (ROOT / "stacks/agentcore_stack.py").read_text(encoding="utf-8")
    cron = (ROOT / "stacks/cron_stack.py").read_text(encoding="utf-8")

    assert "WorkspaceSessionRole" in agentcore
    assert "WorkspaceSessionRoleArn" in agentcore
    assert "workspace_session_role" in agentcore
    assert "self.user_files_bucket.grant_read_write(self.execution_role)" not in agentcore
    assert "WorkspaceCredentialBrokerRole" in agentcore
    assert 'self.execution_role.add_to_policy' in agentcore
    assert "scheduler:" not in agentcore.casefold()
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
    release_cli = (ROOT / "release_tools/cli.py").read_text(encoding="utf-8")
    local = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    e2e = (ROOT / "scripts/e2e-deploy-and-test.sh").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'REQUIRED_REGION = "eu-west-1"' in release_cli
    assert 'exec "${PYTHON}" -I -S "${SCRIPT_DIR}/staging-release.py" "$@"' in deploy
    assert "agentcore deploy" not in deploy.casefold()
    assert "get-agent-runtime" not in deploy
    assert "RuntimeContext" not in deploy
    assert "EVENTBRIDGE_SCHEDULE_GROUP" not in deploy
    assert "CRON_LAMBDA_ARN" not in deploy
    assert "EVENTBRIDGE_ROLE_ARN" not in deploy
    assert 'AWS_TEST_REGION="eu-west-1"' in local
    assert 'REQUIRED_REGION="eu-west-1"' in e2e
    assert 'CronStack(app, "OpenClawCron", env=env)' in app
    assert "runtime_iam_arn=agentcore_stack.runtime_iam_arn" in app
    assert 'REQUIRED_REGION = "eu-west-1"' in app


def test_release_owns_direct_l1_runtime_and_verifies_exact_storage_version() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    stack = (ROOT / "stacks/agentcore_stack.py").read_text(encoding="utf-8")
    adapter = (ROOT / "release_tools/agentcore.py").read_text(encoding="utf-8")
    e2e = (ROOT / "scripts/e2e-deploy-and-test.sh").read_text(encoding="utf-8")
    local = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")

    assert "update_agent_runtime(" not in deploy
    assert "update-agent-runtime-endpoint" not in deploy
    assert "agentcore deploy" not in deploy.casefold()
    assert "agentcore.CfnRuntime(" in stack
    assert "agentcore.CfnRuntimeEndpoint(" in stack
    assert 'container_uri=runtime_image_uri' in stack
    assert 'endpointName=expected_endpoint_name' in adapter
    assert '"$PROJECT_DIR/scripts/verify-agentcore-storage.py"' in e2e
    assert "tests/test_verify_agentcore_storage.py" in local
    assert "WARNING: Failed to configure session storage" not in deploy
    assert "skipping session storage config" not in deploy.casefold()


def test_workspace_bucket_never_expires_the_authoritative_current_objects() -> None:
    source = (ROOT / "stacks/agentcore_stack.py").read_text(encoding="utf-8")

    assert "expiration=Duration.days(user_files_ttl_days)" not in source
    assert re.search(
        r"noncurrent_version_expiration=Duration\.days\(\s*user_files_ttl_days\s*\)",
        source,
    )
    assert "abort_incomplete_multipart_upload_after=Duration.days(7)" in source
    assert "bucket_key_enabled=True" in source


def test_runtime_metadata_validation_is_not_embedded_in_the_shell_shim() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    adapter = (ROOT / "release_tools/agentcore.py").read_text(encoding="utf-8")

    assert "validate_runtime_metadata" not in deploy
    assert "json.loads" not in deploy
    assert "AgentCoreEvidenceAdapter" in adapter
    assert (
        "live runtime configuration differs from reviewed release configuration"
        in adapter
    )
    assert "endpoint was retargeted away from the release version" in adapter
    assert "returned unknown status" in adapter


def test_e2e_non_skip_deploys_canonical_runtime_before_validation(
    tmp_path: Path,
) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--test-filter", "TestSmoke"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    deploy_index = next(i for i, call in enumerate(calls) if call.startswith("DEPLOY"))
    workspace_output_index = next(
        i for i, call in enumerate(calls) if "WorkspaceSessionRoleArn" in call
    )
    assert deploy_index < workspace_output_index
    assert not any(call.startswith(("CDK ", "DOCKER ")) for call in calls)
    assert any("get-agent-runtime" in call for call in calls)
    assert any("list-agent-runtime-endpoints" in call for call in calls)
    assert any("lambda get-function-configuration" in call for call in calls)
    assert any("iam get-role-policy" in call for call in calls)
    pytest_call = next(call for call in calls if call.startswith("PYTHON <-m> <pytest>"))
    assert "not TestSubagent" in pytest_call
    assert "not TestApiKeyManagement" in pytest_call
    assert "not TestSkillManagement" in pytest_call
    assert "TestSmoke" in pytest_call


def test_e2e_skip_deploy_validates_existing_runtime(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)

    completed = subprocess.run(
        ["bash", str(script), "--skip-deploy"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("DEPLOY") for call in calls)
    assert any("WorkspaceSessionRoleArn" in call for call in calls)
    assert any("get-agent-runtime" in call for call in calls)
    assert any("list-agent-runtime-endpoints" in call for call in calls)


def test_e2e_skip_deploy_rejects_runtime_configuration_drift(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)
    env["E2E_FAKE_RUNTIME_ROLE"] = (
        "arn:aws:iam::123456789012:role/unexpected-runtime-role"
    )

    completed = subprocess.run(
        ["bash", str(script), "--skip-deploy"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "runtime execution role mismatch" in completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("PYTHON <-m> <pytest>") for call in calls)


def test_e2e_skip_deploy_rejects_runtime_arn_drift(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)
    env["E2E_FAKE_RUNTIME_ARN"] = (
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
        "agent/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:2"
    )

    completed = subprocess.run(
        ["bash", str(script), "--skip-deploy"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "runtime ARN mismatch" in completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("PYTHON <-m> <pytest>") for call in calls)


def test_e2e_rejects_router_invocation_arn_drift(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)
    env["E2E_FAKE_ROUTER_RUNTIME_ARN"] = TEST_RUNTIME_IAM_ARN

    completed = subprocess.run(
        ["bash", str(script), "--skip-deploy"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "router invocation ARN mismatch" in completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("PYTHON <-m> <pytest>") for call in calls)


def test_e2e_rejects_router_iam_resource_drift(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)
    env["E2E_FAKE_ROUTER_IAM_RESOURCES"] = (
        f"{TEST_RUNTIME_ARN} {TEST_RUNTIME_ARN}/*"
    )

    completed = subprocess.run(
        ["bash", str(script), "--skip-deploy"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "router IAM runtime resources mismatch" in completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("PYTHON <-m> <pytest>") for call in calls)


def test_e2e_option_errors_fail_before_aws(tmp_path: Path) -> None:
    poison_bin = tmp_path / "bin"
    poison_bin.mkdir()
    marker = tmp_path / "aws-called"
    _write_executable(
        poison_bin / "aws",
        '#!/usr/bin/env bash\ntouch "$AWS_POISON_MARKER"\nexit 97\n',
    )
    env = _without_aws_regions()
    env.update(
        {
            "AWS_POISON_MARKER": str(marker),
            "PATH": f"{poison_bin}:{env['PATH']}",
        }
    )

    for args in (["--unknown"], ["--test-filter"]):
        marker.unlink(missing_ok=True)
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts/e2e-deploy-and-test.sh"), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2
        assert "Usage:" in completed.stderr
        assert not marker.exists(), f"AWS was called for invalid arguments: {args}"


def test_e2e_test_filter_remains_one_argument(tmp_path: Path) -> None:
    script, env, call_log = _create_e2e_script_harness(tmp_path)
    injected_marker = tmp_path / "injected"
    requested_filter = f"TestSmoke; touch {injected_marker}"

    completed = subprocess.run(
        [
            "bash",
            str(script),
            "--skip-deploy",
            "--test-filter",
            requested_filter,
        ],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not injected_marker.exists()
    pytest_call = next(
        call
        for call in call_log.read_text(encoding="utf-8").splitlines()
        if call.startswith("PYTHON <-m> <pytest>")
    )
    python_args = re.findall(r"<([^>]*)>", pytest_call)
    filter_arg = python_args[python_args.index("-k") + 1]
    assert requested_filter in filter_arg


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


TEST_RUNTIME_ID = "openclaw_agent-0123456789"
TEST_SOURCE_COMMIT = "a" * 40
TEST_RUNTIME_ENDPOINT_ID = "release_endpoint-0123456789"
TEST_RUNTIME_ENDPOINT_NAME = f"release_{TEST_SOURCE_COMMIT}"
TEST_RUNTIME_VERSION = "1"
TEST_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agent/12345678-1234-1234-1234-123456789abc:1"
)
TEST_RUNTIME_IAM_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    f"runtime/{TEST_RUNTIME_ID}"
)
TEST_RUNTIME_IMAGE_URI = (
    "123456789012.dkr.ecr.eu-west-1.amazonaws.com/"
    "personal-operator/bridge@sha256:" + "a" * 64
)
TEST_CAPABILITY_GATEWAY_ARN = (
    "arn:aws:lambda:eu-west-1:123456789012:function:"
    "personal-operator-capability-gateway"
)
TEST_CAPABILITY_STATE_TABLE_NAME = "personal-operator-capability-state"
TEST_CAPABILITY_STATE_TABLE_ARN = (
    "arn:aws:dynamodb:eu-west-1:123456789012:table/"
    + TEST_CAPABILITY_STATE_TABLE_NAME
)
TEST_CAPABILITY_CATALOG_DIGEST = "b" * 64


def _build_agentcore_stack(
    region: str = "eu-west-1",
    context_overrides: dict | None = None,
    *,
    guardrail_id: str = "",
    guardrail_version: str = "",
    guardrail_arn: str = "",
):
    from aws_cdk import App, Environment, Stack, aws_ec2 as ec2
    from stacks.agentcore_stack import AgentCoreStack

    account = "123456789012"
    context = {
        "runtime_source_commit": TEST_SOURCE_COMMIT,
        "runtime_id": TEST_RUNTIME_ID,
        "runtime_endpoint_id": TEST_RUNTIME_ENDPOINT_ID,
        "runtime_endpoint_name": TEST_RUNTIME_ENDPOINT_NAME,
        "runtime_version": TEST_RUNTIME_VERSION,
        "runtime_arn": TEST_RUNTIME_ARN,
        "runtime_image_uri": TEST_RUNTIME_IMAGE_URI,
        "user_files_ttl_days": "30",
        "enable_browser": "false",
    }
    context.update(context_overrides or {})
    app = App(
        context=context
    )
    env = Environment(account=account, region=region)
    network = Stack(app, f"Network{region.replace('-', '')}", env=env)
    vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=0)
    trusted_endpoint_sg = ec2.SecurityGroup(
        network,
        "TrustedEndpointSecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
    )
    stack = AgentCoreStack(
        app,
        f"AgentCore{region.replace('-', '')}",
        cmk_arn=f"arn:aws:kms:{region}:{account}:key/test-key",
        vpc=vpc,
        private_subnet_ids=["subnet-00000000000000001"],
        workspace_capability_secret_name=(
            "personal-operator/workspace-capability"
        ),
        capability_gateway_function_arn=TEST_CAPABILITY_GATEWAY_ARN,
        trusted_endpoint_security_group=trusted_endpoint_sg,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        guardrail_arn=guardrail_arn,
        env=env,
    )
    return stack


def _synth_agentcore_template(region: str = "eu-west-1") -> dict:
    from aws_cdk.assertions import Template

    stack = _build_agentcore_stack(region)
    return Template.from_stack(stack).to_json()


def test_agentcore_stack_uses_only_persisted_runtime_arn() -> None:
    stack = _build_agentcore_stack()

    assert stack.runtime_source_commit == TEST_SOURCE_COMMIT
    assert stack.runtime_arn == TEST_RUNTIME_ARN
    assert stack.runtime_iam_arn == TEST_RUNTIME_IAM_ARN
    assert stack.runtime_endpoint_id == TEST_RUNTIME_ENDPOINT_ID
    assert stack.runtime_endpoint_name == TEST_RUNTIME_ENDPOINT_NAME
    assert stack.runtime_version == TEST_RUNTIME_VERSION
    assert stack.runtime_image_uri == TEST_RUNTIME_IMAGE_URI


def test_agentcore_stack_allows_only_fully_empty_offline_runtime_context() -> None:
    stack = _build_agentcore_stack(
        context_overrides={
            "runtime_source_commit": "",
            "runtime_id": "",
            "runtime_endpoint_id": "",
            "runtime_endpoint_name": "",
            "runtime_version": "",
            "runtime_arn": "",
            "runtime_image_uri": "",
        }
    )

    assert stack.runtime_arn == "PLACEHOLDER"
    assert stack.runtime_source_commit == "PLACEHOLDER"
    assert stack.runtime_iam_arn == "PLACEHOLDER"
    assert stack.runtime_endpoint_id == "PLACEHOLDER"
    assert stack.runtime_endpoint_name == "PLACEHOLDER"
    assert stack.runtime_version == "PLACEHOLDER"
    assert stack.runtime_image_uri == "PLACEHOLDER"


def test_agentcore_stack_rejects_incomplete_or_noncanonical_runtime_context() -> None:
    invalid_contexts = [
        {"runtime_source_commit": ""},
        {"runtime_arn": ""},
        {"runtime_id": ""},
        {"runtime_endpoint_id": ""},
        {"runtime_endpoint_name": ""},
        {"runtime_version": ""},
        {"runtime_image_uri": ""},
        {
            "runtime_arn": (
                "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
                f"runtime/{TEST_RUNTIME_ID}"
            )
        },
        {
            "runtime_arn": TEST_RUNTIME_ARN.replace(
                ":eu-west-1:123456789012:", ":us-east-1:123456789012:"
            )
        },
        {
            "runtime_arn": TEST_RUNTIME_ARN.replace(
                ":123456789012:", ":999999999999:"
            )
        },
        {"runtime_id": "runtime-test"},
        {"runtime_endpoint_id": "endpoint-test"},
        {"runtime_source_commit": "A" * 40},
        {"runtime_endpoint_name": "DEFAULT"},
        {"runtime_endpoint_name": "release_" + "b" * 40},
        {"runtime_version": "2"},
        {
            "runtime_image_uri": (
                "123456789012.dkr.ecr.eu-west-1.amazonaws.com/"
                "personal-operator/bridge:latest"
            )
        },
        {
            "runtime_image_uri": TEST_RUNTIME_IMAGE_URI.replace(
                "123456789012", "999999999999"
            )
        },
        {
            "runtime_image_uri": TEST_RUNTIME_IMAGE_URI.replace(
                "eu-west-1", "us-east-1"
            )
        },
    ]

    for context_overrides in invalid_contexts:
        try:
            _build_agentcore_stack(context_overrides=context_overrides)
        except ValueError as error:
            assert "runtime" in str(error).casefold()
        else:
            raise AssertionError(
                f"AgentCoreStack accepted invalid runtime context: {context_overrides}"
            )


def _synth_router_template(
    *,
    runtime_arn: str = TEST_RUNTIME_ARN,
    runtime_iam_arn: str = TEST_RUNTIME_IAM_ARN,
    runtime_endpoint_name: str = TEST_RUNTIME_ENDPOINT_NAME,
    registration_open: str = "false",
    capability_state_table_name: str = TEST_CAPABILITY_STATE_TABLE_NAME,
    capability_state_table_arn: str = TEST_CAPABILITY_STATE_TABLE_ARN,
    capability_release_commit: str = TEST_SOURCE_COMMIT,
    capability_catalog_digest: str = TEST_CAPABILITY_CATALOG_DIGEST,
) -> dict:
    from aws_cdk import App, Environment
    from aws_cdk.assertions import Template

    from stacks.router_stack import RouterStack

    account = "123456789012"
    app = App(context={"registration_open": registration_open})
    stack = RouterStack(
        app,
        "Router",
        runtime_arn=runtime_arn,
        runtime_iam_arn=runtime_iam_arn,
        runtime_endpoint_name=runtime_endpoint_name,
        capability_state_table_name=capability_state_table_name,
        capability_state_table_arn=capability_state_table_arn,
        capability_release_commit=capability_release_commit,
        capability_catalog_digest=capability_catalog_digest,
        telegram_token_secret_name="openclaw/channels/telegram",
        slack_token_secret_name="openclaw/channels/slack",
        feishu_token_secret_name="openclaw/channels/feishu",
        webhook_secret_name="openclaw/webhook-secret",
        workspace_capability_secret_name=(
            "personal-operator/workspace-capability"
        ),
        workspace_broker_role_arn=(
            f"arn:aws:iam::{account}:role/"
            "personal-operator-workspace-credential-broker-eu-west-1"
        ),
        workspace_broker_function_name=(
            "personal-operator-workspace-credential-broker"
        ),
        workspace_session_role_arn=(
            f"arn:aws:iam::{account}:role/"
            "openclaw-workspace-session-role-eu-west-1"
        ),
        cmk_arn=f"arn:aws:kms:eu-west-1:{account}:key/test-key",
        user_files_bucket_name="openclaw-user-files-test",
        user_files_bucket_arn=(
            f"arn:aws:s3:::openclaw-user-files-{account}-eu-west-1"
        ),
        trusted_code_asset_root="lambda",
        env=Environment(account=account, region="eu-west-1"),
    )
    return Template.from_stack(stack).to_json()


def test_external_pilot_router_rejects_open_registration():
    try:
        _synth_router_template(registration_open="true")
    except ValueError as error:
        assert "registration" in str(error).casefold()
        assert "closed" in str(error).casefold()
    else:
        raise AssertionError("external pilot accepted open registration")


def test_worker_separates_invocation_arn_from_iam_runtime_resource() -> None:
    template = _synth_router_template()
    resources = template["Resources"].values()
    worker = next(
        resource
        for resource in resources
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName")
        == "personal-operator-telegram-worker"
    )
    policies = [
        statement
        for resource in resources
        if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]
    invocation = next(
        statement
        for statement in policies
        if "bedrock-agentcore:InvokeAgentRuntime"
        in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )

    assert worker["Properties"]["Environment"]["Variables"][
        "AGENTCORE_RUNTIME_ARN"
    ] == TEST_RUNTIME_ARN
    assert worker["Properties"]["Environment"]["Variables"][
        "AGENTCORE_QUALIFIER"
    ] == TEST_RUNTIME_ENDPOINT_NAME
    assert invocation["Resource"] == [
        TEST_RUNTIME_IAM_ARN,
        f"{TEST_RUNTIME_IAM_ARN}/runtime-endpoint/{TEST_RUNTIME_ENDPOINT_NAME}",
    ]
    assert TEST_RUNTIME_ARN not in invocation["Resource"]


def test_router_accepts_only_matching_grammar_or_all_placeholders() -> None:
    _synth_router_template(
        runtime_arn="PLACEHOLDER",
        runtime_iam_arn="PLACEHOLDER",
        runtime_endpoint_name="PLACEHOLDER",
    )
    invalid_values = [
        {
            "runtime_arn": TEST_RUNTIME_IAM_ARN,
            "runtime_iam_arn": TEST_RUNTIME_IAM_ARN,
        },
        {
            "runtime_arn": TEST_RUNTIME_ARN,
            "runtime_iam_arn": TEST_RUNTIME_ARN,
        },
        {
            "runtime_arn": TEST_RUNTIME_ARN.replace("eu-west-1", "us-east-1"),
        },
        {
            "runtime_iam_arn": TEST_RUNTIME_IAM_ARN.replace(
                "123456789012", "999999999999"
            ),
        },
        {"runtime_iam_arn": "PLACEHOLDER"},
        {"runtime_endpoint_name": "PLACEHOLDER"},
    ]
    for overrides in invalid_values:
        try:
            _synth_router_template(**overrides)
        except ValueError as error:
            assert "runtime" in str(error).casefold()
        else:
            raise AssertionError(f"RouterStack accepted invalid runtime values: {overrides}")


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


def test_synthesized_workspace_role_trusts_only_the_credential_broker() -> None:
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
                "personal-operator-workspace-credential-broker-eu-west-1"
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


def test_synthesized_runtime_invokes_only_exact_broker_and_capability_gateway() -> None:
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
    assert [action for action in actions if action.startswith("sts:")] == []
    invoke_statements = [
        statement
        for statement in statements
        if statement.get("Action") == "lambda:InvokeFunction"
    ]
    assert invoke_statements == [
        {
            "Action": "lambda:InvokeFunction",
            "Effect": "Allow",
            "Resource": (
                "arn:aws:lambda:eu-west-1:123456789012:function:"
                "personal-operator-workspace-credential-broker"
            ),
        },
        {
            "Action": "lambda:InvokeFunction",
            "Effect": "Allow",
            "Resource": TEST_CAPABILITY_GATEWAY_ARN,
        },
    ]


def test_synthesized_runtime_has_no_public_egress_and_only_trusted_destinations() -> None:
    template = _synth_agentcore_template()
    egress = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::EC2::SecurityGroupEgress"
    ]

    assert egress
    assert all(rule.get("CidrIp") != "0.0.0.0/0" for rule in egress)
    runtime_rules = [
        rule
        for rule in egress
        if rule.get("Description", "").startswith("Personal Operator runtime")
    ]
    assert len(runtime_rules) == 1
    assert {rule["IpProtocol"] for rule in runtime_rules} == {"tcp"}
    assert {(rule["FromPort"], rule["ToPort"]) for rule in runtime_rules} == {
        (443, 443)
    }
    assert "DestinationSecurityGroupId" in runtime_rules[0]
    assert "DestinationPrefixListId" not in runtime_rules[0]


def test_guardrail_iam_and_runtime_bind_exact_subject_and_version() -> None:
    from aws_cdk.assertions import Template

    guardrail_id = "guardrail1234"
    guardrail_version = "7"
    guardrail_arn = (
        "arn:aws:bedrock:eu-west-1:123456789012:guardrail/guardrail1234"
    )
    stack = _build_agentcore_stack(
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        guardrail_arn=guardrail_arn,
    )
    template = Template.from_stack(stack).to_json()
    _, statements = _role_and_statements(
        template, "openclaw-agentcore-execution-role-eu-west-1"
    )
    apply = [
        statement
        for statement in statements
        if statement.get("Action") == "bedrock:ApplyGuardrail"
    ]
    assert apply == [
        {
            "Action": "bedrock:ApplyGuardrail",
            "Effect": "Allow",
            "Resource": guardrail_arn,
        }
    ]
    runtime = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    )
    environment = runtime["Properties"]["EnvironmentVariables"]
    assert environment["BEDROCK_GUARDRAIL_ID"] == guardrail_id
    assert environment["BEDROCK_GUARDRAIL_VERSION"] == guardrail_version


def test_app_wires_the_exact_guardrail_and_trusted_endpoint_boundary() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "guardrail_id=guardrails_stack.guardrail_id" in source
    assert "guardrail_version=guardrails_stack.guardrail_version" in source
    assert "guardrail_arn=guardrails_stack.guardrail_arn" in source
    assert "trusted_endpoint_security_group=vpc_stack.vpce_sg" in source
    assert "s3_prefix_list_id=" not in source


def test_vpc_exposes_lambda_and_private_bedrock_endpoints_for_runtime_mediation() -> None:
    from aws_cdk import App, Environment
    from aws_cdk.assertions import Template
    from stacks.vpc_stack import VpcStack

    app = App()
    stack = VpcStack(
        app,
        "RuntimeVpc",
        env=Environment(account="123456789012", region="eu-west-1"),
    )
    template = Template.from_stack(stack).to_json()
    endpoints = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::EC2::VPCEndpoint"
    ]
    rendered = json.dumps(endpoints, sort_keys=True, separators=(",", ":"))
    assert "com.amazonaws.eu-west-1.lambda" in rendered
    s3_interface = [
        endpoint
        for endpoint in endpoints
        if endpoint.get("VpcEndpointType") == "Interface"
        and "com.amazonaws.eu-west-1.s3" in json.dumps(endpoint, sort_keys=True)
    ]
    assert len(s3_interface) == 1
    assert s3_interface[0]["PrivateDnsEnabled"] is True
    bedrock = [
        endpoint
        for endpoint in endpoints
        if "bedrock-runtime" in json.dumps(endpoint, sort_keys=True)
    ]
    assert len(bedrock) == 1
    assert bedrock[0]["PrivateDnsEnabled"] is True


def test_runtime_owned_browser_flag_is_rejected_and_never_synthesizes_iam() -> None:
    try:
        _build_agentcore_stack(context_overrides={"enable_browser": "true"})
    except ValueError as error:
        assert "trusted Browser Gateway" in str(error)
    else:
        raise AssertionError("runtime-owned browser authority was accepted")

    template = _synth_agentcore_template()
    _, statements = _role_and_statements(
        template, "openclaw-agentcore-execution-role-eu-west-1"
    )
    actions = _flatten_statement_actions(statements)
    assert not any("browser" in action.casefold() for action in actions)
    assert not any(
        resource["Type"] == "AWS::BedrockAgentCore::BrowserCustom"
        for resource in template["Resources"].values()
    )


def test_synthesized_credential_broker_is_the_sole_workspace_role_assumer() -> None:
    template = _synth_agentcore_template()
    _, broker_statements = _role_and_statements(
        template,
        "personal-operator-workspace-credential-broker-eu-west-1",
    )
    _, runtime_statements = _role_and_statements(
        template, "openclaw-agentcore-execution-role-eu-west-1"
    )

    broker_assume = [
        statement
        for statement in broker_statements
        if statement.get("Action") == "sts:AssumeRole"
    ]
    assert broker_assume == [
        {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Resource": (
                "arn:aws:iam::123456789012:role/"
                "openclaw-workspace-session-role-eu-west-1"
            ),
        }
    ]
    assert not any(
        action.startswith("s3:")
        for action in _flatten_statement_actions(broker_statements)
    )
    assert not any(
        action.startswith("sts:")
        for action in _flatten_statement_actions(runtime_statements)
    )

    broker_kms = [
        statement
        for statement in broker_statements
        if statement.get("Resource")
        == "arn:aws:kms:eu-west-1:123456789012:key/test-key"
        and any(
            action.startswith("kms:")
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        )
    ]
    assert broker_kms == [
        {
            "Action": "kms:Decrypt",
            "Condition": {
                "StringEquals": {
                    "kms:CallerAccount": "123456789012",
                    "kms:ViaService": "secretsmanager.eu-west-1.amazonaws.com",
                }
            },
            "Effect": "Allow",
            "Resource": "arn:aws:kms:eu-west-1:123456789012:key/test-key",
        },
        {
            "Action": ["kms:Decrypt", "kms:DescribeKey"],
            "Condition": {
                "StringEquals": {
                    "kms:CallerAccount": "123456789012",
                    "kms:ViaService": "dynamodb.eu-west-1.amazonaws.com",
                }
            },
            "Effect": "Allow",
            "Resource": "arn:aws:kms:eu-west-1:123456789012:key/test-key",
        },
    ]


def test_synthesized_execution_role_invokes_only_frozen_eu_sonnet_profile() -> None:
    template = _synth_agentcore_template()
    _, statements = _role_and_statements(
        template, "openclaw-agentcore-execution-role-eu-west-1"
    )
    invocation_actions = {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    invocation_statements = [
        statement
        for statement in statements
        if invocation_actions
        & set(
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    ]

    profile_arn = (
        "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/"
        "eu.anthropic.claude-sonnet-4-6"
    )
    destination_regions = {
        "eu-central-1",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-3",
    }
    foundation_model_arns = {
        f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-6"
        for region in destination_regions
    }

    assert len(invocation_statements) == 2
    profile_statement = next(
        statement
        for statement in invocation_statements
        if statement["Resource"] == profile_arn
    )
    model_statement = next(
        statement
        for statement in invocation_statements
        if isinstance(statement["Resource"], list)
    )

    assert set(profile_statement["Action"]) == invocation_actions
    assert "Condition" not in profile_statement
    assert set(model_statement["Action"]) == invocation_actions
    assert set(model_statement["Resource"]) == foundation_model_arns
    assert model_statement["Condition"] == {
        "StringLike": {"bedrock:InferenceProfileArn": profile_arn}
    }

    invocation_resources = [
        resource
        for statement in invocation_statements
        for resource in (
            statement["Resource"]
            if isinstance(statement["Resource"], list)
            else [statement["Resource"]]
        )
    ]
    assert all("*" not in resource for resource in invocation_resources)
    assert all("global." not in resource for resource in invocation_resources)
    assert all(
        not resource.startswith("arn:aws:bedrock:::")
        for resource in invocation_resources
    )


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
                "PYTHON": sys.executable,
                "PYTHONPATH": str(ROOT),
            }
        )
        arguments = ["bash", str(ROOT / relative_script)]
        if relative_script == "scripts/deploy.sh":
            env.pop("PYTHON", None)
            arguments = [
                sys.executable,
                "-I",
                "-S",
                str(ROOT / "scripts/staging-release.py"),
            ]
            arguments.extend(
                [
                    "--preflight",
                    "--journal",
                    str(tmp_path / "journal.json"),
                    "--root",
                    str(ROOT),
                    "--account",
                    "123456789012",
                    "--commit",
                    "a" * 40,
                ]
            )
        completed = subprocess.run(
            arguments,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 1
        assert "must be exactly eu-west-1" in completed.stderr
        assert not marker.exists(), f"{relative_script} called aws before region gate"


def test_e2e_script_rejects_mismatched_private_telegram_identity_before_aws(
    tmp_path: Path,
) -> None:
    poison_bin = tmp_path / "bin"
    poison_bin.mkdir()
    marker = tmp_path / "aws-called"
    _write_executable(
        poison_bin / "aws",
        '#!/usr/bin/env bash\ntouch "$AWS_POISON_MARKER"\nexit 97\n',
    )
    env = _without_aws_regions()
    env.update(
        {
            "AWS_POISON_MARKER": str(marker),
            "E2E_TELEGRAM_CHAT_ID": "10001",
            "E2E_TELEGRAM_USER_ID": "10002",
            "PATH": f"{poison_bin}:{env['PATH']}",
        }
    )

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/e2e-deploy-and-test.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "same positive private Telegram ID" in completed.stderr
    assert not marker.exists()


def test_setup_and_allowlist_scripts_default_to_canonical_region(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "aws-calls"
    _write_executable(
        fake_bin / "aws",
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$AWS_CALL_LOG"\nexit 97\n',
    )
    env = _without_aws_regions()
    env.update(
        {
            "AWS_CALL_LOG": str(call_log),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    invocations = {
        "scripts/setup-telegram.sh": [],
        "scripts/setup-slack.sh": [],
        "scripts/setup-feishu.sh": [],
        "scripts/manage-allowlist.sh": ["list"],
    }

    for relative_script, args in invocations.items():
        call_log.unlink(missing_ok=True)
        completed = subprocess.run(
            ["bash", str(ROOT / relative_script), *args],
            cwd=ROOT,
            env=env,
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 97
        first_call = call_log.read_text(encoding="utf-8").splitlines()[0]
        assert "--region eu-west-1" in first_call, relative_script
        assert "us-west-2" not in first_call, relative_script


def test_setup_and_allowlist_reject_wrong_region_before_aws(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "aws-called"
    _write_executable(
        fake_bin / "aws",
        '#!/usr/bin/env bash\ntouch "$AWS_POISON_MARKER"\nexit 97\n',
    )
    invocations = {
        "scripts/setup-telegram.sh": [],
        "scripts/setup-slack.sh": [],
        "scripts/setup-feishu.sh": [],
        "scripts/manage-allowlist.sh": ["list"],
    }

    for relative_script, args in invocations.items():
        for region_variable in (
            "CDK_DEFAULT_REGION",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        ):
            marker.unlink(missing_ok=True)
            env = _without_aws_regions()
            env.update(
                {
                    region_variable: "us-west-2",
                    "AWS_POISON_MARKER": str(marker),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )
            completed = subprocess.run(
                ["bash", str(ROOT / relative_script), *args],
                cwd=ROOT,
                env=env,
                input="",
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 1, (
                relative_script,
                region_variable,
                completed.stderr,
            )
            assert "must be exactly eu-west-1" in completed.stderr
            assert not marker.exists(), (
                f"{relative_script} called aws before rejecting {region_variable}"
            )


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
        "personal-operator/web-auth",
        "personal-operator/approval-signing",
        "personal-operator/cloudfront-origin-verification",
        "personal-operator/google-readonly-oauth",
        "personal-operator/google-send-oauth",
        "personal-operator/openai-api-key",
        "personal-operator/workspace-capability",
    }
    send_secret = next(
        resource
        for resource in resources
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and resource["Properties"]["Name"]
        == "personal-operator/google-send-oauth"
    )
    assert '"user_id": "REPLACE_ME"' in send_secret["Properties"][
        "GenerateSecretString"
    ]["SecretStringTemplate"]
