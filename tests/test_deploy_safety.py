"""Static and early-exit contracts for the deliberately guarded deploy path."""

from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy.sh"
RELEASE_SCRIPT = ROOT / "scripts" / "test-release-assets.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _run(*args: str, **environment: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        **environment,
    }
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
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
        "build/\n.env\n.env.*\n", encoding="utf-8"
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
    }
    runtime_path = repo / "build" / "runtime-context.json"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(json.dumps(runtime_context), encoding="utf-8")

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


def test_unknown_mode_and_build_mode_fail_before_any_aws_call() -> None:
    unknown = _run("--typo")
    assert unknown.returncode != 0
    assert "unknown deployment mode" in unknown.stderr
    assert "credentials" not in unknown.stdout.casefold()

    build = _run("--phase1", BUILD_MODE="surprise")
    assert build.returncode != 0
    assert "BUILD_MODE" in build.stderr


def test_runtime_modes_fail_closed_before_preflight_or_cloud_calls() -> None:
    for mode in ("--full", "--runtime-only"):
        result = _run(mode)

        assert result.returncode != 0
        assert "immutable AgentCore runtime deployment is not implemented" in result.stderr
        assert "no cloud changes were made" in result.stderr
        assert "PERSONAL_OPERATOR_DEPLOY_ACCOUNT" not in result.stderr
        assert "AWS credentials" not in result.stdout


def test_no_mutable_agentcore_toolkit_deploy_path_remains() -> None:
    source = _source()

    assert '"$AGENTCORE_CLI" deploy' not in source
    assert "Starter Toolkit deploy" not in source


def test_deploy_requires_explicit_account_commit_and_confirmation() -> None:
    source = _source()
    assert "PERSONAL_OPERATOR_DEPLOY_ACCOUNT" in source
    assert "PERSONAL_OPERATOR_DEPLOY_COMMIT" in source
    assert "PERSONAL_OPERATOR_DEPLOY_CONFIRMATION" in source
    assert "aws sts get-caller-identity --query Account" in source
    assert "must match the authenticated STS account" in source
    assert "status --porcelain" in source
    assert "rev-parse HEAD" in source


def test_deploy_requires_immutable_builder_and_global_waf() -> None:
    source = _source()
    assert "TRUSTED_LAMBDA_BUILD_IMAGE" in source
    assert "@sha256:" in source
    assert "cloudfront_web_acl_arn" in source
    assert "arn:aws:wafv2:us-east-1:" in source
    assert "global/webacl/" in source


def test_deploy_requires_commit_bound_immutable_bridge_runtime_image() -> None:
    source = _source()

    assert "PERSONAL_OPERATOR_RUNTIME_IMAGE_URI" in source
    assert "runtime image must be an immutable ECR digest" in source
    assert "aws ecr describe-images" in source
    assert 'expected_tag = f"commit-{commit}"' in source
    assert '"containerUri": expected_runtime_image_uri' in source
    assert 'runtime.get("agentRuntimeArtifact") != expected_artifact' in source
    assert 'agentRuntimeArtifact=runtime["agentRuntimeArtifact"]' not in source


def test_deploy_contains_no_privileged_runtime_builder_fallback() -> None:
    source = _source()
    assert "--privileged" not in source
    assert "tonistiigi/binfmt" not in source
    assert "AGENTCORE_CLI" not in source


def test_runtime_identity_is_commit_bound_without_mutating_tracked_cdk_context() -> None:
    source = _source()
    assert 'RUNTIME_CONTEXT_FILE="$PROJECT_DIR/build/runtime-context.json"' in source
    assert "personal-operator.runtime-context.v3" in source
    assert 'value.get("sourceCommit")' in source
    assert 'runtime_version = value.get("runtimeVersion", "")' in source
    assert 'f"release_{commit}"' in source
    assert 'value.get("runtimeImageUri") != expected_runtime_image_uri' in source
    assert "runtime context is not bound to this release" in source
    assert "runtime context is not bound to the reviewed release image" in source
    assert '-c "runtime_id=$RUNTIME_ID"' in source
    assert '-c "runtime_source_commit=$PERSONAL_OPERATOR_DEPLOY_COMMIT"' in source
    assert '-c "runtime_version=$RUNTIME_VERSION"' in source
    assert '-c "runtime_arn=$RUNTIME_ARN"' in source
    assert '-c "runtime_image_uri=$RUNTIME_IMAGE_URI"' in source
    assert "Updating cdk.json with runtime info" not in source
    assert "cfg['context']['runtime_id']" not in source
    assert "target.write_text" not in source


def test_phase3_binds_endpoint_id_name_and_target_version_before_cdk() -> None:
    source = _source()

    assert '--endpoint-id "$RUNTIME_ENDPOINT_ID"' in source
    assert '--endpoint-name "$RUNTIME_ENDPOINT_NAME"' in source
    assert '"$RUNTIME_VERSION" != "${RUNTIME_ARN##*:}"' in source
    assert 'rsplit(":", 1)[-1] != runtime_version' in source


def test_cdk_requires_human_approval_for_permission_broadening() -> None:
    source = _source()
    assert "--require-approval never" not in source
    assert source.count("--require-approval broadening") == 2


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
    assert "personal-operator.runtime-context.v3" in source
    assert "PERSONAL_OPERATOR_RUNTIME_IMAGE_URI" in source
    assert 'f"release_{commit}"' in source
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
