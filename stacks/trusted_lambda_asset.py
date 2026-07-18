"""Fail-closed selection of the trusted Python Lambda deployment asset."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import stat
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "personal-operator.trusted-lambda-asset.v1"
SYNTHETIC_ACCOUNT = "000000000000"
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_BUILDER_IMAGE = re.compile(r"public\.ecr\.aws/lambda/python@sha256:[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_EXCLUDED_INVENTORY = {"ASSET.sha256", "MANIFEST.json", "SHA256SUMS"}
_CAPABILITY_SOURCE = Path("specs/capabilities")
_CAPABILITY_ASSET = PurePosixPath("capabilities/artifacts")
_REQUIRED_HANDLERS = {
    "router/index.py",
    "worker/index.py",
    "web/index.py",
    "control/index.py",
    "workspace_broker/index.py",
    "capabilities/gateway.py",
    "capabilities/composition.py",
    "capabilities/durable.py",
    "capabilities/artifacts/catalog-v1.json",
}
_REQUIRED_DEPENDENCIES = {
    "boto3",
    "cryptography",
    "google-api-python-client",
    "google-auth",
    "openai",
}
_EXACT_PIN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*"
)


class TrustedLambdaAssetError(RuntimeError):
    """The dependency-bearing Lambda asset is absent or not authenticated."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TrustedLambdaAssetError("trusted Lambda asset has an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise TrustedLambdaAssetError("trusted Lambda asset has an unsafe path")
    return value


def _validate_file_inventory(value: Any, *, source: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        label = "source" if source else "file"
        raise TrustedLambdaAssetError(
            f"trusted Lambda asset {label} inventory is empty"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_keys = {"path", "sha256", "size"} | (set() if source else {"mode"})
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise TrustedLambdaAssetError("trusted Lambda asset inventory is malformed")
        relative = _safe_relative_path(raw.get("path"))
        if relative in seen or relative in _EXCLUDED_INVENTORY:
            raise TrustedLambdaAssetError(
                "trusted Lambda asset inventory has duplicates"
            )
        seen.add(relative)
        digest = raw.get("sha256")
        size = raw.get("size")
        if not isinstance(digest, str) or _HEX_64.fullmatch(digest) is None:
            raise TrustedLambdaAssetError("trusted Lambda asset digest is malformed")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise TrustedLambdaAssetError("trusted Lambda asset size is malformed")
        item: dict[str, Any] = {"path": relative, "sha256": digest, "size": size}
        if not source:
            mode = raw.get("mode")
            if mode not in {"0644", "0755"}:
                raise TrustedLambdaAssetError("trusted Lambda asset mode is malformed")
            item["mode"] = mode
        result.append(item)
    if result != sorted(result, key=lambda item: item["path"]):
        raise TrustedLambdaAssetError("trusted Lambda asset inventory is not canonical")
    return result


def _source_inputs(root: Path) -> list[tuple[Path, str]]:
    lambda_source = root / "lambda"
    inputs = [
        (path, path.relative_to(lambda_source).as_posix())
        for path in lambda_source.rglob("*.py")
        if not path.name.startswith("test_")
        and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
    ]
    inputs.append((lambda_source / "requirements.txt", "requirements.txt"))
    capability_source = root / _CAPABILITY_SOURCE
    catalog = capability_source / "catalog-v1.json"
    schemas = sorted((capability_source / "schemas").glob("*.json"))
    if len(schemas) != 20:
        raise TrustedLambdaAssetError(
            "trusted Lambda capability schema inventory is not exact"
        )
    inputs.extend(
        (
            path,
            (
                _CAPABILITY_ASSET / path.relative_to(capability_source).as_posix()
            ).as_posix(),
        )
        for path in [catalog, *schemas]
    )
    return sorted(inputs, key=lambda item: item[1])


def _source_inventory(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path, relative in _source_inputs(root):
        if not path.is_file() or path.is_symlink():
            raise TrustedLambdaAssetError("trusted Lambda source boundary is invalid")
        payload = path.read_bytes()
        result.append(
            {
                "path": relative,
                "sha256": _sha256(payload),
                "size": len(payload),
            }
        )
    return result


def _validate_hash_lock(path: Path) -> None:
    logical: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    if pending or not logical:
        raise TrustedLambdaAssetError("trusted Lambda requirements lock is incomplete")
    for line in logical:
        tokens = shlex.split(line)
        if not tokens or _EXACT_PIN.fullmatch(tokens[0]) is None or len(tokens) < 2:
            raise TrustedLambdaAssetError("trusted Lambda requirements are not locked")
        if any(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", token) is None
            for token in tokens[1:]
        ):
            raise TrustedLambdaAssetError(
                "trusted Lambda requirements are not hash locked"
            )


def _actual_asset_inventory(asset: Path) -> list[dict[str, Any]]:
    actual: list[dict[str, Any]] = []
    for path in sorted(
        asset.rglob("*"), key=lambda item: item.relative_to(asset).as_posix()
    ):
        if path.is_symlink():
            raise TrustedLambdaAssetError("trusted Lambda asset contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(asset).as_posix()
        if relative in _EXCLUDED_INVENTORY:
            continue
        payload = path.read_bytes()
        actual.append(
            {
                "path": relative,
                "sha256": _sha256(payload),
                "size": len(payload),
                "mode": format(stat.S_IMODE(path.stat().st_mode), "04o"),
            }
        )
    return actual


def _resolve_built_asset(root: Path, asset: Path) -> str | None:
    manifest_path = asset / "MANIFEST.json"
    sums_path = asset / "SHA256SUMS"
    digest_path = asset / "ASSET.sha256"
    if not any(
        path.exists() or path.is_symlink()
        for path in (manifest_path, sums_path, digest_path)
    ):
        return None
    try:
        if asset.is_symlink() or not asset.is_dir():
            raise TrustedLambdaAssetError("trusted Lambda asset contains a symlink")
        for path in (manifest_path, sums_path, digest_path):
            if path.is_symlink() or not path.is_file():
                raise TrustedLambdaAssetError(
                    "trusted Lambda asset metadata is incomplete"
                )
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        recorded_digest = digest_path.read_text(encoding="ascii")
    except TrustedLambdaAssetError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TrustedLambdaAssetError(
            "trusted Lambda asset metadata is unreadable"
        ) from error

    if recorded_digest != _sha256(manifest_bytes) + "\n":
        raise TrustedLambdaAssetError(
            "trusted Lambda asset manifest authentication failed"
        )
    if not isinstance(manifest, dict):
        raise TrustedLambdaAssetError(
            "trusted Lambda asset manifest has the wrong contract"
        )
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("platform") != "linux/arm64"
        or manifest.get("python") != "3.13"
        or manifest.get("requirementsMode") != "sha256-locked"
        or manifest.get("sourceDateEpoch") != 0
        or not isinstance(manifest.get("builderImage"), str)
        or _BUILDER_IMAGE.fullmatch(manifest["builderImage"]) is None
        or not isinstance(manifest.get("builderImageId"), str)
        or _IMAGE_ID.fullmatch(manifest["builderImageId"]) is None
    ):
        raise TrustedLambdaAssetError(
            "trusted Lambda asset manifest has the wrong contract"
        )

    files = _validate_file_inventory(manifest.get("files"), source=False)
    source_files = _validate_file_inventory(manifest.get("sourceFiles"), source=True)
    actual_files = _actual_asset_inventory(asset)
    if files != actual_files:
        raise TrustedLambdaAssetError("trusted Lambda asset file set or bytes changed")
    expected_sums = "".join(f'{item["sha256"]}  {item["path"]}\n' for item in files)
    if sums_path.read_text(encoding="utf-8") != expected_sums:
        raise TrustedLambdaAssetError("trusted Lambda asset checksum inventory changed")
    if manifest.get("payloadBytes") != sum(item["size"] for item in files):
        raise TrustedLambdaAssetError("trusted Lambda asset payload size changed")

    source = root / "lambda"
    requirements = source / "requirements.txt"
    requirements_input = source / "requirements.in"
    if not requirements_input.is_file() or requirements_input.is_symlink():
        raise TrustedLambdaAssetError("trusted Lambda requirements input is invalid")
    _validate_hash_lock(requirements)
    if manifest.get("requirementsSha256") != _sha256(requirements.read_bytes()):
        raise TrustedLambdaAssetError("trusted Lambda asset requirements are stale")
    if manifest.get("requirementsInputSha256") != _sha256(
        requirements_input.read_bytes()
    ):
        raise TrustedLambdaAssetError(
            "trusted Lambda asset requirements input is stale"
        )
    current_source = _source_inventory(root)
    if source_files != current_source:
        raise TrustedLambdaAssetError("trusted Lambda asset source is stale")
    if not _REQUIRED_HANDLERS.issubset({item["path"] for item in source_files}):
        raise TrustedLambdaAssetError("trusted Lambda asset is missing a handler")

    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise TrustedLambdaAssetError("trusted Lambda dependency inventory is empty")
    normalized_names: set[str] = set()
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or set(dependency) != {"name", "version"}
            or not isinstance(dependency["name"], str)
            or not dependency["name"]
            or not isinstance(dependency["version"], str)
            or not dependency["version"]
        ):
            raise TrustedLambdaAssetError(
                "trusted Lambda dependency inventory is malformed"
            )
        normalized_names.add(re.sub(r"[-_.]+", "-", dependency["name"].casefold()))
    if not _REQUIRED_DEPENDENCIES.issubset(normalized_names):
        raise TrustedLambdaAssetError("trusted Lambda asset is missing a dependency")
    return str(asset)


def resolve_trusted_lambda_asset(
    repository_root: Path,
    *,
    account: str | None,
    allow_synthetic_source: bool = False,
) -> str:
    """Return a fully verified asset root or one impossible-account test escape."""

    root = repository_root.resolve(strict=True)
    asset = root / "build" / "trusted-lambda"
    resolved = _resolve_built_asset(root, asset)
    if resolved is not None:
        return resolved

    if allow_synthetic_source and account == SYNTHETIC_ACCOUNT:
        source = root / "lambda"
        if not source.is_dir() or source.is_symlink():
            raise TrustedLambdaAssetError("synthetic Lambda source root is invalid")
        return str(source)

    raise TrustedLambdaAssetError(
        "build/trusted-lambda is missing; run "
        "scripts/build-trusted-lambda-asset.sh build"
    )
