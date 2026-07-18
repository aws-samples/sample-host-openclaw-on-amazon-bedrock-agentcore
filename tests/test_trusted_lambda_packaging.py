from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

import pytest

from release_tools.contracts import ContractError
from release_tools.lambda_asset import (
    build_trusted_lambda_artifacts,
    verify_trusted_lambda_artifact,
)
from stacks.trusted_lambda_asset import (
    SCHEMA,
    TrustedLambdaAssetError,
    resolve_trusted_lambda_asset,
    resolve_trusted_lambda_asset_metadata,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-trusted-lambda-asset.sh"
REQUIREMENTS = ROOT / "lambda" / "requirements.txt"


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

    locked_names = {
        shlex.split(line)[0].split("==", 1)[0].casefold()
        for line in requirement_lines
    }
    assert {
        "boto3",
        "cryptography",
        "google-api-python-client",
        "google-auth",
        "openai",
    }.issubset(locked_names)


def _write_executable(path: pathlib.Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _packaging_subprocess_fixture(
    tmp_path: pathlib.Path, *, existing_asset: bool = False
) -> tuple[pathlib.Path, dict[str, str], pathlib.Path]:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "lambda").mkdir()
    (repository / "release_tools").mkdir()
    (repository / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (repository / "scripts" / SCRIPT.name).chmod(0o755)
    for name in ("__init__.py", "contracts.py", "lambda_asset.py"):
        source = ROOT / "release_tools" / name
        (repository / "release_tools" / name).write_bytes(source.read_bytes())
    (repository / "lambda" / "requirements.txt").write_bytes(
        REQUIREMENTS.read_bytes()
    )
    if existing_asset:
        asset = repository / "build" / "trusted-lambda"
        asset.mkdir(parents=True)
        (asset / "existing-release").write_text("retain me\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Packaging Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "packaging@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"], cwd=repository, check=True
    )

    log = tmp_path / "docker.jsonl"
    command_log = tmp_path / "forbidden-commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "docker",
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

args = sys.argv[1:]
stdin = sys.stdin.read()
with pathlib.Path(os.environ["FAKE_DOCKER_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{"argv": args, "stdin": stdin}}, sort_keys=True) + "\\n")
if args == ["version"]:
    raise SystemExit(0)
if args[:1] == ["pull"]:
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    format_value = args[args.index("--format") + 1]
    if format_value == "{{{{.Os}}}}/{{{{.Architecture}}}}":
        print("linux/arm64")
    elif ".RepoDigests" in format_value:
        print(os.environ["TRUSTED_LAMBDA_BUILD_IMAGE"])
    elif format_value == "{{{{.Id}}}}":
        print("sha256:" + "7" * 64)
    else:
        raise SystemExit(91)
    raise SystemExit(0)
if args[:1] != ["run"]:
    raise SystemExit(92)
if os.environ.get("FAKE_DOCKER_FAIL_VERIFY") == "1" and "lambda_asset verify" in stdin:
    raise SystemExit(93)
if "lambda_asset build" in stdin:
    output_mount = next(
        value for value in args if value.endswith(":/output-parent:rw")
    )
    output_parent = pathlib.Path(output_mount.removesuffix(":/output-parent:rw"))
    output = output_parent / args[-1]
    output.mkdir()
    for name in ("MANIFEST.json", "SHA256SUMS", "ASSET.sha256", "trusted-lambda.zip"):
        (output / name).write_text(name + "\\n", encoding="utf-8")
raise SystemExit(0)
""",
    )
    for name in ("aws", "cdk"):
        _write_executable(
            fake_bin / name,
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0 $*\" >> \"$FORBIDDEN_COMMAND_LOG\"\n"
            "exit 97\n",
        )
    if existing_asset:
        _write_executable(fake_bin / "mv", "#!/bin/sh\nexit 88\n")

    python_bin = str(pathlib.Path(sys.executable).parent)
    environment = {
        **os.environ,
        "PATH": os.pathsep.join(
            (str(fake_bin), python_bin, "/usr/local/bin", "/usr/bin", "/bin")
        ),
        "FAKE_DOCKER_LOG": str(log),
        "FORBIDDEN_COMMAND_LOG": str(command_log),
        "TRUSTED_LAMBDA_BUILD_IMAGE": (
            "public.ecr.aws/lambda/python@sha256:" + "6" * 64
        ),
        "AWS_ACCESS_KEY_ID": "host-access-must-not-cross",
        "AWS_SECRET_ACCESS_KEY": "host-secret-must-not-cross",
        "AWS_SESSION_TOKEN": "host-session-must-not-cross",
        "HOME": str(tmp_path / "host-home"),
    }
    return repository, environment, log


def _run_packaging_build(
    repository: pathlib.Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(repository / "scripts" / SCRIPT.name), "build"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _docker_records(path: pathlib.Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_build_executes_only_the_immutable_lambda_313_arm64_boundary(
    tmp_path: pathlib.Path,
) -> None:
    repository, environment, log = _packaging_subprocess_fixture(tmp_path)

    completed = _run_packaging_build(repository, environment)

    assert completed.returncode == 0, completed.stderr
    records = _docker_records(log)
    pull = next(record for record in records if record["argv"][:1] == ["pull"])
    assert pull["argv"] == [
        "pull",
        "--platform",
        "linux/arm64",
        environment["TRUSTED_LAMBDA_BUILD_IMAGE"],
    ]
    platform_probe = next(
        record
        for record in records
        if record["argv"][:1] == ["run"]
        and '"ID=amzn"' in " ".join(str(value) for value in record["argv"])
    )
    assert "--platform" in platform_probe["argv"]
    assert platform_probe["argv"][platform_probe["argv"].index("--platform") + 1] == (
        "linux/arm64"
    )
    probe_program = " ".join(str(value) for value in platform_probe["argv"])
    assert "sys.version_info[:2] == (3, 13)" in probe_program
    assert '"ID=amzn"' in probe_program


def test_container_commands_do_not_forward_credentials_or_invoke_deploy(
    tmp_path: pathlib.Path,
) -> None:
    repository, environment, log = _packaging_subprocess_fixture(tmp_path)

    completed = _run_packaging_build(repository, environment)

    assert completed.returncode == 0, completed.stderr
    records = _docker_records(log)
    rendered = json.dumps(records, sort_keys=True)
    for secret in (
        environment["AWS_ACCESS_KEY_ID"],
        environment["AWS_SECRET_ACCESS_KEY"],
        environment["AWS_SESSION_TOKEN"],
    ):
        assert secret not in rendered
    assert str(pathlib.Path(environment["HOME"]) / ".aws") not in rendered
    assert str(pathlib.Path(environment["HOME"]) / ".config") not in rendered
    assert "AWS_SESSION_TOKEN" not in rendered
    assert not (tmp_path / "forbidden-commands.log").exists()
    for record in (item for item in records if item["argv"][:1] == ["run"]):
        argv = record["argv"]
        assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
        assert "--security-opt" in argv
        assert "no-new-privileges" in argv
        assert "AWS_EC2_METADATA_DISABLED=true" in argv


def test_verification_container_is_offline_and_executes_exact_import_gate(
    tmp_path: pathlib.Path,
) -> None:
    repository, environment, log = _packaging_subprocess_fixture(tmp_path)

    completed = _run_packaging_build(repository, environment)

    assert completed.returncode == 0, completed.stderr
    verify = next(
        record
        for record in _docker_records(log)
        if record["argv"][:1] == ["run"]
        and "release_tools.lambda_asset verify" in str(record["stdin"])
    )
    assert "--network" in verify["argv"]
    assert verify["argv"][verify["argv"].index("--network") + 1] == "none"
    assert any(str(value).endswith(":/asset:ro") for value in verify["argv"])
    assert any(str(value).endswith(":/workspace:ro") for value in verify["argv"])
    program = str(verify["stdin"])
    for import_name in (
        "boto3",
        "cryptography",
        "googleapiclient",
        "openai",
        "control.index",
        "control.composition",
        "router.index",
        "web.index",
        "web.composition",
        "worker.index",
        "workspace_broker.index",
    ):
        assert f"import {import_name}" in program
    assert "-m pip check" in program


def test_failed_republication_preserves_the_existing_verified_asset(
    tmp_path: pathlib.Path,
) -> None:
    repository, environment, _ = _packaging_subprocess_fixture(
        tmp_path, existing_asset=True
    )
    existing = repository / "build" / "trusted-lambda" / "existing-release"

    completed = _run_packaging_build(repository, environment)

    assert completed.returncode != 0
    assert existing.read_text(encoding="utf-8") == "retain me\n"


def _write_source_and_payload(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    source = root / "lambda"
    payload = root / "payload"
    for relative in (
        "router/index.py",
        "worker/index.py",
        "web/index.py",
        "control/index.py",
        "workspace_broker/index.py",
    ):
        source_path = source / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# {relative}\n", encoding="utf-8")
        payload_path = payload / relative
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(source_path.read_bytes())
        payload_path.chmod(0o644)
    (source / "requirements.in").write_text(
        "boto3==1.0\ncryptography==1.0\ngoogle-api-python-client==1.0\n"
        "google-auth==1.0\nopenai==1.0\n",
        encoding="utf-8",
    )
    (source / "requirements.txt").write_text(
        "boto3==1.0 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    (payload / "requirements.txt").write_bytes(
        (source / "requirements.txt").read_bytes()
    )
    for name in (
        "boto3",
        "cryptography",
        "google-api-python-client",
        "google-auth",
        "openai",
    ):
        metadata = payload / f"{name.replace('-', '_')}-1.0.dist-info" / "METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
            encoding="utf-8",
        )
        metadata.chmod(0o644)
    return source, payload


def test_lambda_zip_is_byte_identical_across_independent_builds(
    tmp_path: pathlib.Path,
) -> None:
    source, payload = _write_source_and_payload(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    kwargs = {
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "builder_image": "public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        "builder_image_id": "sha256:" + "3" * 64,
    }

    first_manifest = build_trusted_lambda_artifacts(payload, source, first, **kwargs)
    second_manifest = build_trusted_lambda_artifacts(payload, source, second, **kwargs)

    assert first_manifest == second_manifest
    assert (first / "trusted-lambda.zip").read_bytes() == (
        second / "trusted-lambda.zip"
    ).read_bytes()
    assert (first / "MANIFEST.json").read_bytes() == (
        second / "MANIFEST.json"
    ).read_bytes()
    assert first_manifest.SCHEMA == "personal-operator.trusted-lambda-asset.v2"
    assert first_manifest.platform == "linux/arm64"
    assert first_manifest.python == "3.13"
    assert first_manifest.archive_name == "trusted-lambda.zip"
    with __import__("zipfile").ZipFile(first / "trusted-lambda.zip") as archive:
        names = archive.namelist()
    assert names == sorted(names)
    assert {"MANIFEST.json", "ASSET.sha256", "SHA256SUMS"}.isdisjoint(names)


@pytest.mark.parametrize("missing", ["handler", "dependency"])
def test_verifier_rejects_missing_required_runtime_inventory(
    tmp_path: pathlib.Path,
    missing: str,
) -> None:
    source, payload = _write_source_and_payload(tmp_path)
    if missing == "handler":
        for root in (source, payload):
            (root / "workspace_broker" / "index.py").unlink()
            (root / "workspace_broker").rmdir()
    else:
        metadata = payload / "openai-1.0.dist-info" / "METADATA"
        metadata.unlink()
        metadata.parent.rmdir()
    output = tmp_path / "asset"
    build_trusted_lambda_artifacts(
        payload,
        source,
        output,
        source_commit="a" * 40,
        source_tree="b" * 40,
        builder_image="public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        builder_image_id="sha256:" + "3" * 64,
    )

    with pytest.raises(ContractError, match="handler|dependency"):
        verify_trusted_lambda_artifact(
            output,
            source,
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )


def test_cdk_resolves_authenticated_zip_and_exact_custom_hash(
    tmp_path: pathlib.Path,
) -> None:
    source, payload = _write_source_and_payload(tmp_path)
    output = tmp_path / "build" / "trusted-lambda"
    manifest = build_trusted_lambda_artifacts(
        payload,
        source,
        output,
        source_commit="a" * 40,
        source_tree="b" * 40,
        builder_image="public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        builder_image_id="sha256:" + "3" * 64,
    )

    resolved = resolve_trusted_lambda_asset_metadata(
        tmp_path,
        account="123456789012",
        expected_commit="a" * 40,
        expected_tree="b" * 40,
    )

    assert resolved.path == str(output / "trusted-lambda.zip")
    assert resolved.asset_hash == manifest.archive_sha256
    assert resolve_trusted_lambda_asset(
        tmp_path,
        account="123456789012",
        expected_commit="a" * 40,
        expected_tree="b" * 40,
    ) == str(output / "trusted-lambda.zip")


def test_cdk_rejects_zip_or_release_identity_mutation(tmp_path: pathlib.Path) -> None:
    source, payload = _write_source_and_payload(tmp_path)
    output = tmp_path / "build" / "trusted-lambda"
    build_trusted_lambda_artifacts(
        payload,
        source,
        output,
        source_commit="a" * 40,
        source_tree="b" * 40,
        builder_image="public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        builder_image_id="sha256:" + "3" * 64,
    )

    with pytest.raises(TrustedLambdaAssetError, match="commit"):
        resolve_trusted_lambda_asset_metadata(
            tmp_path,
            account="123456789012",
            expected_commit="f" * 40,
            expected_tree="b" * 40,
        )

    archive = output / "trusted-lambda.zip"
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(TrustedLambdaAssetError, match="archive"):
        resolve_trusted_lambda_asset_metadata(
            tmp_path,
            account="123456789012",
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )


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

    (asset / "trusted-lambda.zip").write_bytes(b"not-a-zip")
    with pytest.raises(TrustedLambdaAssetError, match="fields|manifest|archive|artifact"):
        resolve_trusted_lambda_asset(
            tmp_path,
            account="123456789012",
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )


def _write_valid_asset(repository_root: pathlib.Path) -> pathlib.Path:
    source, payload = _write_source_and_payload(repository_root)
    asset = repository_root / "build" / "trusted-lambda"
    build_trusted_lambda_artifacts(
        payload,
        source,
        asset,
        source_commit="a" * 40,
        source_tree="b" * 40,
        builder_image="public.ecr.aws/lambda/python@sha256:" + "2" * 64,
        builder_image_id="sha256:" + "3" * 64,
    )
    return asset


def test_cdk_asset_resolution_accepts_fresh_authenticated_arm64_python313_build(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)

    assert resolve_trusted_lambda_asset(
        tmp_path,
        account="123456789012",
        expected_commit="a" * 40,
        expected_tree="b" * 40,
    ) == str(asset / "trusted-lambda.zip")


def test_cdk_asset_resolution_rejects_empty_stale_or_extra_assets(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)
    (tmp_path / "lambda" / "web" / "index.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(TrustedLambdaAssetError, match="source"):
        resolve_trusted_lambda_asset(
            tmp_path,
            account="123456789012",
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )

    second = tmp_path / "second"
    asset = _write_valid_asset(second)
    (asset / "unexpected.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(TrustedLambdaAssetError, match="external files"):
        resolve_trusted_lambda_asset(
            second,
            account="123456789012",
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )


def test_cdk_asset_resolution_rejects_symlinks(tmp_path) -> None:
    asset = _write_valid_asset(tmp_path)
    (asset / "escape.py").symlink_to(tmp_path / "lambda" / "web" / "index.py")
    with pytest.raises(TrustedLambdaAssetError, match="non-file"):
        resolve_trusted_lambda_asset(
            tmp_path,
            account="123456789012",
            expected_commit="a" * 40,
            expected_tree="b" * 40,
        )


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
    release_gate_source = (ROOT / "scripts" / "test-release-assets.sh").read_text(
        encoding="utf-8"
    )

    assert "resolve_trusted_lambda_asset_metadata(" in app_source
    assert app_source.count("trusted_code_asset_root=trusted_lambda_asset.path") == 2
    assert app_source.count("trusted_code_asset_hash=trusted_lambda_asset.asset_hash") == 2
    assert 'web_asset_root=str(repository_root / "web" / "dist")' in app_source
    for source in (router_source, web_source):
        assert "asset_hash=trusted_code_asset_hash" in source
        assert "asset_hash_type=AssetHashType.CUSTOM" in source
    build = '"${REPO_ROOT}/scripts/build-trusted-lambda-asset.sh" build'
    verify = '"${REPO_ROOT}/scripts/build-trusted-lambda-asset.sh" verify'
    assert build in release_gate_source and verify in release_gate_source
    assert release_gate_source.index(build) < release_gate_source.index("app.py")
    assert release_gate_source.index(verify) < release_gate_source.index("app.py")
    assert "cdk deploy" not in release_gate_source
    assert '"${SCRIPT_DIR}/staging-release.py" "$@"' in deploy_source
    assert "PersonalOperatorWeb" in app_source
