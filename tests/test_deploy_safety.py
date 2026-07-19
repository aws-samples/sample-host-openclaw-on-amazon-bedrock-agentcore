"""Static and early-exit contracts for the deliberately guarded deploy path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from release_tools import cli as release_cli
from release_tools.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy.sh"
RELEASE_SCRIPT = ROOT / "scripts" / "test-release-assets.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _run(*args: str, **environment: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(ROOT),
        **environment,
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(ROOT / "scripts" / "staging-release.py"),
            *args,
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _release_gate_harness(
    tmp_path: Path,
    *,
    mutation: str = "none",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RELEASE_SCRIPT, scripts / RELEASE_SCRIPT.name)
    shutil.copytree(
        ROOT / "release_tools",
        repo / "release_tools",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    helper = ROOT / "scripts" / "hermetic-aws-env.sh"
    if helper.exists():
        shutil.copy2(helper, scripts / helper.name)

    _write_executable(
        scripts / "build-trusted-lambda-asset.sh",
        "#!/usr/bin/env bash\nset -eu\nexit 0\n",
    )
    if mutation == "tracked":
        effect = "printf '\\n# phase mutation\\n' >> \"$ROOT/app.py\""
    elif mutation == "untracked":
        effect = "mkdir -p \"$ROOT/lambda\"; printf 'injected = True\\n' > \"$ROOT/lambda/injected.py\""
    elif mutation == "ignored_web_env":
        effect = "mkdir -p \"$ROOT/web\"; printf 'VITE_SECRET=poison\\n' > \"$ROOT/web/.env.production\""
    else:
        effect = ":"
    _write_executable(
        scripts / "test-local.sh",
        (
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "ROOT=\"$(cd \"$(dirname \"$0\")/..\" && pwd)\"\n"
            f"{effect}\n"
        ),
    )
    _write_executable(
        tmp_path / "bin" / "docker",
        "#!/usr/bin/env bash\nset -eu\n[[ \"${1:-}\" = version ]]\n",
    )
    _write_executable(
        tmp_path / "bin" / "npm",
        "#!/usr/bin/env bash\nexit 0\n",
    )

    observed = tmp_path / "observed-aws-env.json"
    (repo / "app.py").write_text(
        """from pathlib import Path
import json
import os

out = Path(os.environ["CDK_OUTDIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "Fixture.template.json").write_text("{}\\n", encoding="utf-8")
(out / "AwsSolutions--Fixture-NagReport.csv").write_text(
    "Compliance,Rule ID,Resource ID,Rule Info\\n", encoding="utf-8"
)
observed = os.environ.get("OBSERVED_AWS_ENV_FILE")
if observed:
    Path(observed).write_text(
        json.dumps(
            {
                "HOME": os.environ.get("HOME"),
                "AWS_CONFIG_FILE": os.environ.get("AWS_CONFIG_FILE"),
                "AWS_SHARED_CREDENTIALS_FILE": os.environ.get(
                    "AWS_SHARED_CREDENTIALS_FILE"
                ),
                "AWS_EC2_METADATA_DISABLED": os.environ.get(
                    "AWS_EC2_METADATA_DISABLED"
                ),
                "AWS_WEB_IDENTITY_TOKEN_FILE": os.environ.get(
                    "AWS_WEB_IDENTITY_TOKEN_FILE"
                ),
                "AWS_ROLE_ARN": os.environ.get("AWS_ROLE_ARN"),
                "AWS_CONTAINER_CREDENTIALS_FULL_URI": os.environ.get(
                    "AWS_CONTAINER_CREDENTIALS_FULL_URI"
                ),
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": os.environ.get(
                    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"
                ),
            },
            sort_keys=True,
        )
        + "\\n",
        encoding="utf-8",
    )
""",
        encoding="utf-8",
    )
    (repo / "cdk.json").write_text('{"context": {}}\n', encoding="utf-8")
    (repo / ".gitignore").write_text(
        "build/\n.env\n.env.*\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    account = "123456789012"
    region = "eu-west-1"
    image = (
        f"{account}.dkr.ecr.{region}.amazonaws.com/"
        f"personal-operator/bridge@sha256:{'4' * 64}"
    )
    role_arn = (
        f"arn:aws:iam::{account}:role/"
        f"openclaw-agentcore-execution-role-{region}"
    )
    runtime_configuration = {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": image}
        },
        "authorizerConfiguration": {},
        "environmentVariables": {
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
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
    runtime_context = {
        "schema": "personal-operator.runtime-context.v3",
        "sourceCommit": commit,
        "account": account,
        "region": region,
        "runtimeId": "Runtime-ABCDEFGHIJ",
        "runtimeEndpointId": "Endpoint-ABCDEFGHIJ",
        "runtimeEndpointName": f"release_{commit}",
        "runtimeArn": (
            f"arn:aws:bedrock-agentcore:{region}:{account}:agent/"
            "12345678-1234-1234-1234-123456789abc:7"
        ),
        "runtimeVersion": "7",
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
    runtime_path = repo / "build" / "runtime-context.json"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(
        json.dumps(runtime_context, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    poison_home = tmp_path / "poison-home"
    (poison_home / ".aws").mkdir(parents=True)
    (poison_home / ".aws" / "credentials").write_text(
        "[default]\naws_access_key_id=AKIAABCDEFGHIJKLMNOP\n"
        "aws_secret_access_key=poison\n",
        encoding="utf-8",
    )
    web_identity = tmp_path / "poison-web-identity-token"
    web_identity.write_text("poison", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(poison_home),
        "PYTHON": sys.executable,
        "PERSONAL_OPERATOR_RELEASE_ACCOUNT": account,
        "PERSONAL_OPERATOR_RELEASE_COMMIT": commit,
        "PERSONAL_OPERATOR_RUNTIME_CONTEXT_FILE": str(runtime_path),
        "PERSONAL_OPERATOR_RUNTIME_IMAGE_URI": image,
        "TRUSTED_LAMBDA_BUILD_IMAGE": (
            "public.ecr.aws/lambda/python@sha256:" + "5" * 64
        ),
        "OBSERVED_AWS_ENV_FILE": str(observed),
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(web_identity),
        "AWS_ROLE_ARN": f"arn:aws:iam::{account}:role/poison",
        "AWS_ROLE_SESSION_NAME": "poison",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9/poison",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/poison",
    }
    completed = subprocess.run(
        ["/bin/bash", str(scripts / RELEASE_SCRIPT.name)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, observed


def test_unknown_mode_fails_before_any_aws_call() -> None:
    unknown = _run("--typo")
    assert unknown.returncode != 0
    assert "--preflight --phase --resume --status --rollback" in unknown.stderr
    assert "credentials" not in unknown.stdout.casefold()


def test_legacy_modes_are_absent_from_the_new_release_cli() -> None:
    for mode in ("--full", "--runtime-only", "--phase1", "--phase3"):
        result = _run(mode)
        assert result.returncode != 0
        assert "--preflight --phase --resume --status --rollback" in result.stderr


def test_no_mutable_agentcore_toolkit_deploy_path_remains() -> None:
    source = _source() + (ROOT / "release_tools/cli.py").read_text(encoding="utf-8")

    assert "agentcore deploy" not in source.casefold()
    assert "AGENTCORE_CLI" not in source
    assert "update-agent-runtime-endpoint" not in source


def test_deploy_is_only_an_exec_compatibility_shim() -> None:
    source = _source()
    assert 'exec "${PYTHON}" -I -S "${SCRIPT_DIR}/staging-release.py" "$@"' in source
    assert "aws " not in source
    assert "cdk " not in source
    assert "docker " not in source
    assert "RuntimeContext" not in source
    assert source.count("exec ") == 1


def test_release_entrypoint_rejects_nonisolated_python_and_python_override(
    tmp_path: Path,
) -> None:
    direct = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "staging-release.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert direct.returncode != 0
    assert "isolated" in direct.stderr.casefold()

    overridden = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHON": sys.executable,
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert overridden.returncode != 0
    assert "python override" in overridden.stderr.casefold()


def test_isolated_release_entrypoint_ignores_pythonpath_sitecustomize_and_fake_boto3(
    tmp_path: Path,
) -> None:
    poison = tmp_path / "poison"
    poison.mkdir()
    marker = tmp_path / "loaded"
    (poison / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('sitecustomize', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (poison / "boto3.py").write_text(
        "raise RuntimeError('unreviewed boto3 loaded')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(ROOT / "scripts" / "staging-release.py"),
            "--help",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(poison),
            "PYTHONHOME": str(poison),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    assert "unreviewed boto3" not in completed.stderr


def test_deploy_shim_pins_path_before_resolving_its_own_location(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "shadow-ran"
    attacker = tmp_path / "attacker"
    shadow = tmp_path / "shadow" / "dirname"
    _write_executable(
        shadow,
        "#!/bin/sh\n"
        f": > {str(marker)!r}\n"
        f"printf '%s\\n' {str(attacker / 'scripts')!r}\n",
    )
    _write_executable(
        attacker / ".venv" / "bin" / "python",
        "#!/bin/sh\n"
        f": > {str(marker)!r}\n"
        "exit 0\n",
    )

    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        env={
            "PATH": f"{shadow.parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not marker.exists()
    assert "python environment is missing" in completed.stderr.casefold()


def test_release_environment_declares_login_capable_boto3() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "boto3[crt]==1.43.50" in requirements.splitlines()


def test_cli_preflight_executes_exact_git_identity_checks_without_aws(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("release fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"],
        cwd=repository,
        check=True,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
    ).strip()
    wrong_args = SimpleNamespace(
        root=repository,
        account="123456789012",
        region="eu-west-1",
        commit="0" * 40,
        tree="",
    )
    with pytest.raises(release_cli.ReleaseCliError, match="exact Git HEAD"):
        release_cli._preflight_identity(wrong_args)

    (repository / "untracked.txt").write_text("drift\n", encoding="utf-8")
    dirty_args = SimpleNamespace(
        root=repository,
        account="123456789012",
        region="eu-west-1",
        commit=head,
        tree="",
    )
    with pytest.raises(release_cli.ReleaseCliError, match="clean worktree"):
        release_cli._preflight_identity(dirty_args)


def test_cli_exposes_the_frozen_linear_phase_surface() -> None:
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    for value in (
        "foundation",
        "image",
        "runtime",
        "endpoint",
        "context",
        "consumer-changesets",
        "consumers",
        "verify",
    ):
        assert value in result.stdout
    for option in ("--preflight", "--phase", "--resume", "--status", "--rollback"):
        assert option in result.stdout


def test_deploy_contains_no_privileged_runtime_builder_fallback() -> None:
    source = _source() + (ROOT / "release_tools/cli.py").read_text(encoding="utf-8")
    assert "--privileged" not in source
    assert "tonistiigi/binfmt" not in source
    assert "AGENTCORE_CLI" not in source


def test_deploy_shim_contains_no_embedded_runtime_context_parser() -> None:
    source = _source()
    assert "personal-operator.runtime-context.v3" not in source
    assert "runtimeImageUri" not in source
    assert "json.loads" not in source
    assert "python3 -" not in source


def test_credentials_are_discovered_only_inside_confirmed_phase_execution() -> None:
    source = (ROOT / "release_tools/cli.py").read_text(encoding="utf-8")
    assert "def _discover_account" in source
    assert "def _prepare_composer" in source
    assert "journal.begin_mutation" in source
    phase_body = source.split("def _run_phase(", 1)[1].split("def _read_evidence", 1)[0]
    assert phase_body.index("journal.begin_mutation") < phase_body.index(
        "_prepare_composer("
    )
    prepare_body = source.split("def _prepare_composer(", 1)[1].split(
        "def _observe_and_reconcile(", 1
    )[0]
    assert "_discover_account(" in prepare_body
    assert "def _preflight" in source


def test_no_release_path_disables_human_approval() -> None:
    source = _source() + (ROOT / "release_tools/cli.py").read_text(encoding="utf-8")
    assert "--require-approval never" not in source


def test_deploy_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_release_gate_is_offline_real_account_shaped_and_docker_backed() -> None:
    source = RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert "PERSONAL_OPERATOR_RELEASE_ACCOUNT" in source
    assert '!= "000000000000"' in source
    assert "PERSONAL_OPERATOR_RELEASE_COMMIT" in source
    assert "TRUSTED_LAMBDA_BUILD_IMAGE" in source
    assert "build-trusted-lambda-asset.sh\" build" in source
    assert "build-trusted-lambda-asset.sh\" verify" in source
    assert "PERSONAL_OPERATOR_RUNTIME_CONTEXT_FILE" in source
    assert "-m release_tools.release_assets" in source
    assert "personal-operator.runtime-context.v3" not in source
    assert "PERSONAL_OPERATOR_RUNTIME_IMAGE_URI" in source
    assert "RuntimeContextV3" in (
        ROOT / "release_tools/release_assets.py"
    ).read_text(encoding="utf-8")
    assert 'CDK_CONTEXT_JSON="${CDK_CONTEXT_JSON}"' in source
    assert "PERSONAL_OPERATOR_SYNTH_SOURCE_ASSET" not in source
    assert "run_with_hermetic_aws_env" in source
    assert "cdk deploy" not in source
    assert "aws " not in source
    syntax = subprocess.run(
        ["/bin/bash", "-n", str(RELEASE_SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_release_gate_rechecks_exact_commit_after_tracked_phase_mutation(
    tmp_path: Path,
) -> None:
    result, _ = _release_gate_harness(tmp_path, mutation="tracked")

    assert result.returncode != 0
    assert "worktree changed during release verification" in result.stderr
    assert "verified offline" not in result.stdout


def test_release_gate_rechecks_exact_commit_after_untracked_phase_mutation(
    tmp_path: Path,
) -> None:
    result, _ = _release_gate_harness(tmp_path, mutation="untracked")

    assert result.returncode != 0
    assert "worktree changed during release verification" in result.stderr
    assert "verified offline" not in result.stdout


def test_release_gate_rejects_ignored_vite_environment_build_input(
    tmp_path: Path,
) -> None:
    result, _ = _release_gate_harness(tmp_path, mutation="ignored_web_env")

    assert result.returncode != 0
    assert "ignored web environment input" in result.stderr
    assert "verified offline" not in result.stdout


def test_release_synthesis_ignores_all_poisoned_aws_credential_sources(
    tmp_path: Path,
) -> None:
    result, observed_path = _release_gate_harness(tmp_path)

    assert result.returncode == 0, result.stderr
    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    observed_home = Path(observed.pop("HOME")).resolve()
    assert observed == {
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": None,
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": None,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_ROLE_ARN": None,
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_WEB_IDENTITY_TOKEN_FILE": None,
    }
    assert observed_home != (tmp_path / "poison-home").resolve()
    assert observed_home.name == "home"
    assert observed_home.parent.name.startswith("personal-operator-release-cdk.")


def test_local_and_release_synth_share_the_hermetic_aws_wrapper() -> None:
    local_source = (ROOT / "scripts" / "test-local.sh").read_text(encoding="utf-8")
    release_source = RELEASE_SCRIPT.read_text(encoding="utf-8")

    for source in (local_source, release_source):
        assert "hermetic-aws-env.sh" in source
        assert "run_with_hermetic_aws_env" in source


def test_release_success_does_not_claim_the_bridge_image_was_attested() -> None:
    source = RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert "Lambda/web assets verified offline" in source
    assert "AgentCore runtime image was not built or attested" in source
    assert "Release assets verified offline" not in source
