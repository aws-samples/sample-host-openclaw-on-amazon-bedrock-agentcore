from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shlex
import stat
import subprocess

import pytest

from stacks.trusted_lambda_asset import (
    SCHEMA,
    TrustedLambdaAssetError,
    resolve_trusted_lambda_asset,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-trusted-lambda-asset.sh"
REQUIREMENTS = ROOT / "lambda" / "requirements.txt"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_packaging_script_is_valid_bash_and_help_needs_no_docker() -> None:
    syntax = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "build/trusted-lambda" in help_result.stdout
    assert "build-trusted-lambda-asset.sh verify" in help_result.stdout


def _logical_requirements() -> list[str]:
    requirement_lines: list[str] = []
    pending = ""
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        requirement_lines.append(pending)
        pending = ""
    assert not pending
    return requirement_lines


def test_deployment_requirements_are_transitively_sha256_locked() -> None:
    requirement_lines = _logical_requirements()
    assert requirement_lines
    exact_pin = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
        r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*$"
    )
    for line in requirement_lines:
        tokens = shlex.split(line)
        assert exact_pin.fullmatch(tokens[0]), line
        assert tokens[1:], line
        assert all(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", token)
            for token in tokens[1:]
        ), line

    locked_names = {shlex.split(line)[0].split("==", 1)[0].casefold() for line in requirement_lines}
    assert {
        "boto3",
        "cryptography",
        "google-api-python-client",
        "google-auth",
        "openai",
    }.issubset(locked_names)


def test_build_fails_closed_onto_lambda_python_313_arm64() -> None:
    script = _script_text()
    assert 'readonly PLATFORM="linux/arm64"' in script
    assert 'TRUSTED_LAMBDA_BUILD_IMAGE' in script
    assert 'public.ecr.aws/lambda/python@sha256:' in script
    assert 'public.ecr.aws/lambda/python:3.13 |' not in script
    assert 'docker pull --platform "${PLATFORM}"' in script
    assert '"$(id -u):$(id -g)"' in script
    assert "refusing a host-native build" in script
    assert 'platform.machine() in {"aarch64", "arm64"}' in script
    assert 'sys.version_info[:2] == (3, 13)' in script
    assert '"ID=amzn" in os_release' in script


def test_container_boundary_does_not_forward_credentials_or_deploy() -> None:
    script = _script_text()
    assert '--volume "${HOME}/.aws' not in script
    assert '--volume "${HOME}/.config' not in script
    assert "${AWS_ACCESS_KEY_ID" not in script
    assert "${AWS_SECRET_ACCESS_KEY" not in script
    assert "${AWS_SESSION_TOKEN" not in script
    assert "--env AWS_SESSION_TOKEN" not in script
    assert "--env AWS_ACCESS_KEY_ID=packaging-placeholder" in script
    assert "--env AWS_SECRET_ACCESS_KEY=packaging-placeholder" in script
    assert "cdk deploy" not in script
    assert "aws lambda" not in script
    assert "--env-file" not in script
    assert "AWS_EC2_METADATA_DISABLED=true" in script
    assert "--cap-drop ALL" in script
    assert "--security-opt no-new-privileges" in script
    assert '"${REPO_ROOT}:/workspace:ro"' in script


def test_build_is_atomic_normalized_and_inventory_backed() -> None:
    script = _script_text()
    assert '.trusted-lambda.$$.tmp' in script
    assert 'verify_asset_in_container "${staging_dir}"' in script
    assert 'mv "${staging_dir}" "${ASSET_DIR}"' in script
    assert "SOURCE_DATE_EPOCH=0" in script
    assert "--no-compile" in script
    assert "--only-binary=:all:" in script
    assert "--isolated" in script
    assert "--index-url https://pypi.org/simple" in script
    assert "--require-hashes" in script
    assert "MANIFEST.json" in script
    assert "SHA256SUMS" in script
    assert "ASSET.sha256" in script
    assert '"requirementsSha256"' in script
    assert '"payloadBytes"' in script
    assert "250 MiB unzipped limit" in script
    assert '"dependencies"' in script


def test_verify_is_offline_and_checks_required_provider_imports() -> None:
    script = _script_text()
    verify_body = script.split("verify_asset_in_container()", maxsplit=1)[1].split(
        "build_asset()", maxsplit=1
    )[0]
    assert "--network none" in verify_body
    assert '"${asset_dir}:/asset:ro"' in verify_body
    assert "import cryptography" in verify_body
    assert "import googleapiclient" in verify_body
    assert "import openai" in verify_body
    assert "-m pip check" in verify_body
    assert "actual_files != manifest.get(\"files\")" in verify_body
    assert "import boto3" in verify_body
    assert "import router.index" in verify_body
    assert "import worker.index" in verify_body
    assert "import web.index" in verify_body
    assert "import control.index" in verify_body
    assert "import control.composition" in verify_body
    assert "import web.composition" in verify_body
    assert "import workspace_broker.index" in verify_body
    assert '"workspace_broker/index.py"' in verify_body
    assert "import capabilities.gateway" in verify_body
    assert '"capabilities/gateway.py"' in verify_body


def test_broker_and_capability_gateway_are_in_every_asset_gate() -> None:
    local_gate = (ROOT / "scripts" / "test-local.sh").read_text(encoding="utf-8")
    resolver = (ROOT / "stacks" / "trusted_lambda_asset.py").read_text(
        encoding="utf-8"
    )
    packaging = _script_text()

    assert "lambda/workspace_broker" in local_gate
    assert '"workspace_broker/index.py"' in resolver
    assert '"workspace_broker/index.py"' in packaging
    assert "import workspace_broker.index" in packaging
    assert "lambda/capabilities" in local_gate
    assert '"capabilities/gateway.py"' in resolver
    assert '"capabilities/gateway.py"' in packaging
    assert "import capabilities.gateway" in packaging


def test_cdk_hook_root_is_unambiguous() -> None:
    script = _script_text()
    assert 'readonly ASSET_DIR="${BUILD_DIR}/trusted-lambda"' in script
    assert "CDK code asset root: build/trusted-lambda" in script


def test_cdk_asset_resolution_rejects_missing_or_unauthenticated_build(tmp_path) -> None:
    (tmp_path / "lambda").mkdir()
    with pytest.raises(TrustedLambdaAssetError, match="build/trusted-lambda"):
        resolve_trusted_lambda_asset(tmp_path, account="123456789012")

    asset = tmp_path / "build" / "trusted-lambda"
    asset.mkdir(parents=True)
    manifest = {
        "schema": SCHEMA,
        "platform": "linux/arm64",
        "python": "3.13",
        "files": [],
        "dependencies": [],
    }
    (asset / "MANIFEST.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    (asset / "SHA256SUMS").write_text("", encoding="utf-8")
    (asset / "ASSET.sha256").write_text("0" * 64 + "\n", encoding="ascii")

    with pytest.raises(TrustedLambdaAssetError, match="authentication"):
        resolve_trusted_lambda_asset(tmp_path, account="123456789012")


def _write_valid_asset(repository_root: pathlib.Path) -> pathlib.Path:
    source = repository_root / "lambda"
    for relative in (
        "router/index.py",
        "worker/index.py",
        "web/index.py",
        "control/index.py",
        "workspace_broker/index.py",
        "capabilities/gateway.py",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    (source / "requirements.in").write_text(
        "boto3==1.43.50\ncryptography==49.0.0\n"
        "google-api-python-client==2.198.0\ngoogle-auth==2.56.0\n"
        "openai==2.46.0\n",
        encoding="utf-8",
    )
    (source / "requirements.txt").write_text(
        "boto3==1.43.50 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )

    asset = repository_root / "build" / "trusted-lambda"
    asset.mkdir(parents=True)
    source_files = []
    files = []
    for path in sorted(source.rglob("*.py")) + [source / "requirements.txt"]:
        relative = path.relative_to(source).as_posix()
        target = asset / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        target.chmod(0o644)
        payload = target.read_bytes()
        source_files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
        files.append(
            {
                **source_files[-1],
                "mode": format(stat.S_IMODE(target.stat().st_mode), "04o"),
            }
        )
    files.sort(key=lambda item: item["path"])
    source_files.sort(key=lambda item: item["path"])
    manifest = {
        "schema": SCHEMA,
        "platform": "linux/arm64",
        "python": "3.13",
        "builderImage": "public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        "builderImageId": "sha256:" + "3" * 64,
        "requirementsMode": "sha256-locked",
        "requirementsSha256": hashlib.sha256(
            (source / "requirements.txt").read_bytes()
        ).hexdigest(),
        "requirementsInputSha256": hashlib.sha256(
            (source / "requirements.in").read_bytes()
        ).hexdigest(),
        "sourceDateEpoch": 0,
        "payloadBytes": sum(item["size"] for item in files),
        "sourceFiles": source_files,
        "files": files,
        "dependencies": [
            {"name": name, "version": "1.0"}
            for name in (
                "boto3",
                "cryptography",
                "google-api-python-client",
                "google-auth",
                "openai",
            )
        ],
    }
    payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    (asset / "MANIFEST.json").write_bytes(payload)
    (asset / "SHA256SUMS").write_text(
        "".join(f'{item["sha256"]}  {item["path"]}\n' for item in files),
        encoding="utf-8",
    )
    (asset / "ASSET.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii"
    )
    return asset


def test_cdk_asset_resolution_accepts_fresh_authenticated_arm64_python313_build(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)

    assert resolve_trusted_lambda_asset(
        tmp_path, account="123456789012"
    ) == str(asset)


def test_cdk_asset_resolution_rejects_empty_stale_or_extra_assets(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)
    (tmp_path / "lambda" / "web" / "index.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(TrustedLambdaAssetError, match="source"):
        resolve_trusted_lambda_asset(tmp_path, account="123456789012")

    second = tmp_path / "second"
    asset = _write_valid_asset(second)
    (asset / "unexpected.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(TrustedLambdaAssetError, match="file set"):
        resolve_trusted_lambda_asset(second, account="123456789012")


def test_cdk_asset_resolution_rejects_symlinks(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)
    (asset / "escape.py").symlink_to(tmp_path / "lambda" / "web" / "index.py")
    with pytest.raises(TrustedLambdaAssetError, match="symlink"):
        resolve_trusted_lambda_asset(tmp_path, account="123456789012")


def test_source_only_synth_escape_is_limited_to_impossible_test_account(tmp_path) -> None:
    source = tmp_path / "lambda"
    source.mkdir()

    with pytest.raises(TrustedLambdaAssetError):
        resolve_trusted_lambda_asset(
            tmp_path,
            account="123456789012",
            allow_synthetic_source=True,
        )
    assert resolve_trusted_lambda_asset(
        tmp_path,
        account="000000000000",
        allow_synthetic_source=True,
    ) == str(source)


def test_app_and_deploy_path_require_the_trusted_asset_for_real_stacks() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    router_source = (ROOT / "stacks" / "router_stack.py").read_text(
        encoding="utf-8"
    )
    web_source = (ROOT / "stacks" / "web_stack.py").read_text(encoding="utf-8")
    deploy_source = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "resolve_trusted_lambda_asset(" in app_source
    assert app_source.count("trusted_code_asset_root=trusted_lambda_asset_root") == 3
    assert 'web_asset_root=str(repository_root / "web" / "dist")' in app_source
    assert "_lambda.Code.from_asset(trusted_code_asset_root)" in router_source
    assert web_source.count(
        "_lambda.Code.from_asset(trusted_code_asset_root)"
    ) == 3
    build = '"$PROJECT_DIR/scripts/build-trusted-lambda-asset.sh" build'
    verify = '"$PROJECT_DIR/scripts/build-trusted-lambda-asset.sh" verify'
    deploy = "cdk deploy"
    assert build in deploy_source and verify in deploy_source
    assert deploy_source.index(build) < deploy_source.index(deploy)
    assert deploy_source.index(verify) < deploy_source.index(deploy)
    assert "PersonalOperatorCapabilities" in deploy_source
    assert "PersonalOperatorWeb" in deploy_source
