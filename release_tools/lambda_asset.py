"""Deterministic trusted Lambda ZIP v2 builder and verifier."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import io
import importlib.metadata
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile

from release_tools.contracts import ContractError, TrustedLambdaAssetV2


ARCHIVE_NAME = "trusted-lambda.zip"
MANIFEST_NAME = "MANIFEST.json"
ASSET_DIGEST_NAME = "ASSET.sha256"
CHECKSUMS_NAME = "SHA256SUMS"
ARTIFACT_FILES = {
    ARCHIVE_NAME,
    MANIFEST_NAME,
    ASSET_DIGEST_NAME,
    CHECKSUMS_NAME,
}
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_UNZIPPED_BYTES = 250 * 1024 * 1024
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_REQUIRED_HANDLERS = {
    "router/index.py",
    "worker/index.py",
    "web/index.py",
    "control/index.py",
    "workspace_broker/index.py",
}
_REQUIRED_DEPENDENCIES = {
    "boto3",
    "cryptography",
    "google-api-python-client",
    "google-auth",
    "openai",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _files(root: Path) -> list[dict[str, object]]:
    if not root.is_dir() or root.is_symlink():
        raise ContractError("trusted Lambda payload root is invalid")
    result: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ContractError("trusted Lambda payload contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ContractError("trusted Lambda payload contains a special file")
        payload = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(payload),
                "size": len(payload),
                "mode": format(stat.S_IMODE(path.stat().st_mode), "04o"),
            }
        )
    if not result:
        raise ContractError("trusted Lambda payload is empty")
    return result


def _source_files(root: Path) -> list[dict[str, object]]:
    paths = [
        path
        for path in root.rglob("*.py")
        if not path.name.startswith("test_")
        and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
    ]
    paths.append(root / "requirements.txt")
    result: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.is_symlink():
            raise ContractError("trusted Lambda source inventory is invalid")
        payload = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(payload),
                "size": len(payload),
            }
        )
    return result


def _dependencies(payload_root: Path) -> list[dict[str, str]]:
    dependencies = sorted(
        (
            {"name": distribution.metadata["Name"], "version": distribution.version}
            for distribution in importlib.metadata.distributions(path=[str(payload_root)])
            if distribution.metadata.get("Name")
        ),
        key=lambda item: (item["name"].casefold(), item["version"]),
    )
    if not dependencies:
        raise ContractError("trusted Lambda dependency inventory is empty")
    return dependencies


def _write_deterministic_zip(
    payload_root: Path,
    archive_path: Path,
    files: list[dict[str, object]],
) -> None:
    with zipfile.ZipFile(
        archive_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for item in files:
            relative = str(item["path"])
            info = zipfile.ZipInfo(relative, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (int(str(item["mode"]), 8) & 0xFFFF) << 16
            archive.writestr(
                info,
                (payload_root / relative).read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    archive_path.chmod(0o644)
    os.utime(archive_path, (0, 0), follow_symlinks=False)


def build_trusted_lambda_artifacts(
    payload_root: Path,
    source_root: Path,
    output_root: Path,
    *,
    source_commit: str,
    source_tree: str,
    builder_image: str,
    builder_image_id: str,
) -> TrustedLambdaAssetV2:
    """Build a deterministic ZIP and an external authenticated v2 manifest."""

    payload = Path(payload_root).resolve(strict=True)
    source = Path(source_root).resolve(strict=True)
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=False)
    files = _files(payload)
    payload_bytes = sum(int(item["size"]) for item in files)
    if payload_bytes > MAX_UNZIPPED_BYTES:
        raise ContractError("trusted Lambda asset exceeds the 250 MiB unzipped limit")
    source_files = _source_files(source)
    packaged_by_path = {str(item["path"]): item for item in files}
    for item in source_files:
        packaged = packaged_by_path.get(str(item["path"]))
        if packaged is None or any(
            packaged[field] != item[field] for field in ("sha256", "size")
        ):
            raise ContractError(
                f"packaged source differs from repository source: {item['path']}"
            )
    archive_path = output / ARCHIVE_NAME
    _write_deterministic_zip(payload, archive_path, files)
    archive_payload = archive_path.read_bytes()
    manifest = TrustedLambdaAssetV2.from_mapping(
        {
            "schema": TrustedLambdaAssetV2.SCHEMA,
            "sourceCommit": source_commit,
            "sourceTree": source_tree,
            "platform": "linux/arm64",
            "architecture": "arm64",
            "python": "3.13",
            "builderImage": builder_image,
            "builderImageId": builder_image_id,
            "requirementsMode": "sha256-locked",
            "requirementsSha256": _sha256(
                (source / "requirements.txt").read_bytes()
            ),
            "requirementsInputSha256": _sha256(
                (source / "requirements.in").read_bytes()
            ),
            "sourceDateEpoch": 0,
            "payloadBytes": payload_bytes,
            "archiveName": ARCHIVE_NAME,
            "archiveBytes": len(archive_payload),
            "archiveSha256": _sha256(archive_payload),
            "dependencies": _dependencies(payload),
            "sourceFiles": source_files,
            "files": files,
        }
    )
    manifest_payload = manifest.to_bytes()
    (output / MANIFEST_NAME).write_bytes(manifest_payload)
    (output / CHECKSUMS_NAME).write_text(
        "".join(f'{item["sha256"]}  {item["path"]}\n' for item in files),
        encoding="utf-8",
    )
    (output / ASSET_DIGEST_NAME).write_text(
        _sha256(manifest_payload) + "\n", encoding="ascii"
    )
    for name in ARTIFACT_FILES:
        path = output / name
        path.chmod(0o644)
        os.utime(path, (0, 0), follow_symlinks=False)
    return manifest


def _artifact_root_is_exact(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ContractError("trusted Lambda artifact root is invalid")
    names: set[str] = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ContractError("trusted Lambda artifact contains a non-file")
        names.add(path.name)
    if names != ARTIFACT_FILES:
        raise ContractError("trusted Lambda artifact has the wrong external files")


def _verify_zip_payload(
    archive_payload: bytes, manifest: TrustedLambdaAssetV2
) -> None:
    if (
        len(archive_payload) != manifest.archive_bytes
        or _sha256(archive_payload) != manifest.archive_sha256
    ):
        raise ContractError("trusted Lambda archive bytes changed")
    expected = {item.path: item for item in manifest.files}
    try:
        with zipfile.ZipFile(io.BytesIO(archive_payload)) as archive:
            infos = archive.infolist()
            if [info.filename for info in infos] != sorted(expected):
                raise ContractError("trusted Lambda ZIP inventory is not canonical")
            if len(infos) != len(expected):
                raise ContractError("trusted Lambda ZIP inventory contains duplicates")
            for info in infos:
                parsed = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or parsed.is_absolute()
                    or ".." in parsed.parts
                    or parsed.as_posix() != info.filename
                    or info.filename in ARTIFACT_FILES
                ):
                    raise ContractError("trusted Lambda ZIP contains an unsafe path")
                item = expected[info.filename]
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.date_time != ZIP_TIMESTAMP
                    or info.create_system != 3
                    or info.compress_type != zipfile.ZIP_DEFLATED
                    or format(mode, "04o") != item.mode
                ):
                    raise ContractError("trusted Lambda ZIP metadata is not deterministic")
                payload = archive.read(info)
                if len(payload) != item.size or _sha256(payload) != item.sha256:
                    raise ContractError("trusted Lambda ZIP payload changed")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise ContractError("trusted Lambda archive is invalid") from error


def _verify_zip(archive_path: Path, manifest: TrustedLambdaAssetV2) -> None:
    _verify_zip_payload(archive_path.read_bytes(), manifest)


def verify_trusted_lambda_artifact(
    artifact_root: Path,
    source_root: Path,
    *,
    expected_commit: str,
    expected_tree: str,
) -> TrustedLambdaAssetV2:
    """Verify external manifest, exact release identity, source, and ZIP bytes."""

    root = Path(artifact_root)
    source = Path(source_root)
    _artifact_root_is_exact(root)
    manifest_payload = (root / MANIFEST_NAME).read_bytes()
    manifest = TrustedLambdaAssetV2.from_bytes(manifest_payload)
    if manifest.source_commit != expected_commit:
        raise ContractError("trusted Lambda source commit differs from the release")
    if manifest.source_tree != expected_tree:
        raise ContractError("trusted Lambda source tree differs from the release")
    if (root / ASSET_DIGEST_NAME).read_text(encoding="ascii") != (
        _sha256(manifest_payload) + "\n"
    ):
        raise ContractError("trusted Lambda manifest authentication failed")
    if manifest.requirements_sha256 != _sha256(
        (source / "requirements.txt").read_bytes()
    ) or manifest.requirements_input_sha256 != _sha256(
        (source / "requirements.in").read_bytes()
    ):
        raise ContractError("trusted Lambda requirements are stale")
    current_source = _source_files(source)
    if current_source != [item.to_mapping() for item in manifest.source_files]:
        raise ContractError("trusted Lambda source inventory is stale")
    if not _REQUIRED_HANDLERS.issubset({item.path for item in manifest.source_files}):
        raise ContractError("trusted Lambda source inventory is missing a handler")
    normalized_dependencies = {
        item.name.casefold().replace("_", "-").replace(".", "-")
        for item in manifest.dependencies
    }
    if not _REQUIRED_DEPENDENCIES.issubset(normalized_dependencies):
        raise ContractError("trusted Lambda dependency inventory is incomplete")
    expected_sums = "".join(
        f"{item.sha256}  {item.path}\n" for item in manifest.files
    )
    if (root / CHECKSUMS_NAME).read_text(encoding="utf-8") != expected_sums:
        raise ContractError("trusted Lambda checksum inventory changed")
    _verify_zip(root / manifest.archive_name, manifest)
    return manifest


def extract_verified_archive(
    artifact_root: Path,
    destination: Path,
    manifest: TrustedLambdaAssetV2,
) -> None:
    """Extract only already-verified regular ZIP entries into an empty directory."""

    target = Path(destination)
    target.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(Path(artifact_root) / manifest.archive_name) as archive:
        for item in manifest.files:
            output = target / item.path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(item.path))
            output.chmod(int(item.mode, 8))


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ContractError("trusted Lambda artifact contains a non-file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ContractError("trusted Lambda artifact contains a non-file")
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _artifact_snapshot(root: Path) -> dict[str, bytes]:
    _artifact_root_is_exact(root)
    return {
        name: _read_regular_bytes(root / name) for name in sorted(ARTIFACT_FILES)
    }


def _verify_artifact_snapshot(snapshot: dict[str, bytes]) -> None:
    if set(snapshot) != ARTIFACT_FILES:
        raise ContractError("trusted Lambda artifact has the wrong external files")
    manifest_payload = snapshot[MANIFEST_NAME]
    manifest = TrustedLambdaAssetV2.from_bytes(manifest_payload)
    if snapshot[ASSET_DIGEST_NAME] != (
        _sha256(manifest_payload) + "\n"
    ).encode("ascii"):
        raise ContractError("trusted Lambda manifest authentication failed")
    expected_sums = "".join(
        f"{item.sha256}  {item.path}\n" for item in manifest.files
    ).encode("utf-8")
    if snapshot[CHECKSUMS_NAME] != expected_sums:
        raise ContractError("trusted Lambda checksum inventory changed")
    _verify_zip_payload(snapshot[manifest.archive_name], manifest)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durably_verify_staging(staging: Path) -> None:
    snapshot = _artifact_snapshot(staging)
    _verify_artifact_snapshot(snapshot)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for name, expected in snapshot.items():
        descriptor = os.open(staging / name, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError("trusted Lambda staging contains a non-file")
            if _read_descriptor(descriptor) != expected:
                raise ContractError(
                    "trusted Lambda staging changed during durable verification"
                )
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _read_descriptor(descriptor) != expected:
                raise ContractError(
                    "trusted Lambda staging changed during durable verification"
                )
        finally:
            os.close(descriptor)
    _fsync_directory(staging)
    if _artifact_snapshot(staging) != snapshot:
        raise ContractError(
            "trusted Lambda staging changed during durable verification"
        )


def _atomic_rename_no_replace(source: Path, destination: Path) -> None:
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise ContractError("trusted Lambda no-replace primitive is unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise ContractError(
                "trusted Lambda no-replace primitive is unavailable"
            ) from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        try:
            rename = library.renamex_np
        except AttributeError as error:
            raise ContractError(
                "trusted Lambda no-replace primitive is unavailable"
            ) from error
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, _RENAME_EXCL)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ContractError(
            "trusted Lambda destination already exists; refusing to replace it"
        )
    unsupported = {errno.ENOSYS, errno.EINVAL}
    unsupported.update(
        value
        for value in (
            getattr(errno, "ENOTSUP", None),
            getattr(errno, "EOPNOTSUPP", None),
        )
        if value is not None
    )
    if error_number in unsupported:
        raise ContractError("trusted Lambda no-replace primitive is unavailable")
    raise OSError(error_number, os.strerror(error_number), destination)


def publish_trusted_lambda_artifact(
    staging_root: Path,
    destination_root: Path,
) -> None:
    """Durably publish one verified directory with atomic no-replace semantics."""

    staging = Path(staging_root)
    destination = Path(destination_root)
    if staging.parent.resolve(strict=True) != destination.parent.resolve(strict=True):
        raise ContractError("trusted Lambda publication must stay on one filesystem")
    if not staging.is_dir() or staging.is_symlink():
        raise ContractError("trusted Lambda staging directory is invalid")
    _durably_verify_staging(staging)
    try:
        _atomic_rename_no_replace(staging, destination)
    except OSError as error:
        raise ContractError("trusted Lambda publication was not atomic") from error
    try:
        _fsync_directory(destination.parent)
    except OSError as error:
        raise ContractError(
            "trusted Lambda publication durability is uncertain"
        ) from error


def _cli() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--payload", type=Path, required=True)
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--source-commit", required=True)
    build.add_argument("--source-tree", required=True)
    build.add_argument("--builder-image", required=True)
    build.add_argument("--builder-image-id", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--source", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-tree", required=True)
    verify.add_argument("--extract", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--staging", type=Path, required=True)
    publish.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "build":
        manifest = build_trusted_lambda_artifacts(
            arguments.payload,
            arguments.source,
            arguments.output,
            source_commit=arguments.source_commit,
            source_tree=arguments.source_tree,
            builder_image=arguments.builder_image,
            builder_image_id=arguments.builder_image_id,
        )
    elif arguments.command == "verify":
        manifest = verify_trusted_lambda_artifact(
            arguments.artifact,
            arguments.source,
            expected_commit=arguments.expected_commit,
            expected_tree=arguments.expected_tree,
        )
        if arguments.extract is not None:
            extract_verified_archive(arguments.artifact, arguments.extract, manifest)
    else:
        publish_trusted_lambda_artifact(arguments.staging, arguments.destination)
        print("published trusted Lambda artifact", file=sys.stderr)
        return 0
    print(
        f"verified {len(manifest.files)} files and "
        f"{len(manifest.dependencies)} distributions",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except (ContractError, OSError) as error:
        print(f"trusted Lambda artifact: {error}", file=sys.stderr)
        raise SystemExit(1) from error
