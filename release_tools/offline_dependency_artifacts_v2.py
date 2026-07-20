"""Deterministic offline dependency artifacts for the reviewed runtime build.

The real acquisition entry point is intentionally inert unless its explicit
integration gate is set.  It performs no AWS call and accepts no credential,
cache, registry, executable, image, version, platform, or digest override.
Large package-manager stores are validated, hashed, copied, and archived as
retained files; their bytes are never assembled in memory.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import re
import select
import shutil
import socket
import socketserver
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
from typing import BinaryIO, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


AUDITED_OPENCLAW_VERSION = "2026.7.2"
AUDITED_OPENCLAW_COMMIT = "4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"
AUDITED_OPENCLAW_TREE = "33ee4a213f9b97795ac592b74b82789c5120fab5"
OPENCLAW_PNPM_VERSION = "11.2.2"
OPENCLAW_PNPM_DISTRIBUTION_SHA512 = (
    "36e6621fad506178936455e70247b8808ef4ec25797a9f437a93281a020484e2"
    "607f6a469a22e982987c3dbb8866e3071514ab10a4a1749e06edcd1ec118436f"
)
OPENCLAW_PACKAGE_MANAGER = (
    "pnpm@11.2.2+sha512." + OPENCLAW_PNPM_DISTRIBUTION_SHA512
)
BRIDGE_NPM_VERSION = "11.12.1"
BRIDGE_NPM_DISTRIBUTION_SHA512 = (
    "cdca14b85d647b3192028d02aadbe82d75f79a446aceea9874be98e6d768f20e"
    "bd3555770a48d0e9906106007877bbc690f715e9372f2e2dc644a3c3157fb14c"
)
BRIDGE_LOCK_SHA256 = (
    "5a72f4f98b9191229513cc1d342ca8de0cd933939e48f5e489b616750ab053f0"
)
NODE_BASE_IMAGE = (
    "public.ecr.aws/docker/library/node:24.15.0-slim@sha256:"
    "4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
)
PLATFORM = "linux/arm64"
NODE_VERSION = "v24.15.0"
SOURCE_DATE_EPOCH = 0

_PNPM_URL = "https://registry.npmjs.org/pnpm/-/pnpm-11.2.2.tgz"
_NPM_URL = "https://registry.npmjs.org/npm/-/npm-11.12.1.tgz"
_SCHEMA = "personal-operator.offline-dependency-cache.v1"
_RESULT_SCHEMA = "personal-operator.offline-dependency-artifact-result.v2"
_SET_SCHEMA = "personal-operator.offline-dependency-artifact-set.v2"
_INTEGRATION_GATE = "PERSONAL_OPERATOR_RUN_OFFLINE_DEPENDENCY_ACQUISITION"
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
_MAX_DISTRIBUTION_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_REGISTRY_HOST = "registry.npmjs.org"
_REGISTRY_PORT = 443
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_ID = re.compile(r"[0-9a-f]{40}")
_STORE_FILE = re.compile(r"[0-9a-f]{126}(?:-exec)?")
_INTEGRITY_TOKEN = re.compile(r"sha512-[A-Za-z0-9+/]{86}==")
_REGISTRY_TARBALL_PATH = re.compile(r"/[A-Za-z0-9@._~+/\-]+\.tgz")
_LOCK_INTEGRITY_STANDALONE = re.compile(
    rb"^\s+integrity:\s*['\"]?(sha512-[A-Za-z0-9+/]{86}==)['\"]?\s*$"
)
_LOCK_INTEGRITY_INLINE = re.compile(
    rb"^\s+resolution:\s*\{integrity:\s*(sha512-[A-Za-z0-9+/]{86}==)\}\s*$"
)
_FORBIDDEN_PNPM_SOURCE = re.compile(
    rb"(?i)(?:^|[\s\[{,(])(?:tarball|https?|git|git\+https?|git\+ssh|ssh|github|file):"
)
_LIFECYCLE_LINE = re.compile(
    rb"(?mi)^\s+(?:requiresBuild|preinstall|install|postinstall|prepare):\s*(?:true|[^\s]+)\s*$"
)
_FORBIDDEN_ARCHIVE_PARTS = {
    ".npmrc",
    "_logs",
    "logs",
    "credentials",
    "secrets",
}
_LIFECYCLE_KEYS = {
    "hasInstallScript",
    "preinstall",
    "install",
    "postinstall",
    "prepare",
}


class ArtifactGenerationError(ValueError):
    """The dependency artifact cannot be independently reproduced."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ArtifactGenerationError("canonical result is invalid") from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_path(path: Path, algorithm: str) -> tuple[str, int]:
    digest = hashlib.new(algorithm)
    size = 0
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ArtifactGenerationError("retained artifact is not regular")
        with path.open("rb", buffering=0) as source:
            while True:
                chunk = source.read(_COPY_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_ARTIFACT_BYTES:
                    raise ArtifactGenerationError("retained artifact is unbounded")
                digest.update(chunk)
    except OSError as error:
        raise ArtifactGenerationError("retained artifact is unreadable") from error
    return digest.hexdigest(), size


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_exclusive(path: Path, *, parents: bool = False) -> None:
    if path.exists() or path.is_symlink():
        raise ArtifactGenerationError("fresh output path already exists")
    try:
        path.mkdir(mode=0o700, parents=parents, exist_ok=False)
    except OSError as error:
        raise ArtifactGenerationError("fresh output path cannot be created") from error


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if (
        not path
        or not path.isascii()
        or "\\" in path
        or len(path.encode("utf-8")) > 4096
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ArtifactGenerationError("artifact path is unsafe")
    return candidate


def _validate_integrity(value: object) -> str:
    if not isinstance(value, str) or _INTEGRITY_TOKEN.fullmatch(value) is None:
        raise ArtifactGenerationError("lock integrity is invalid")
    try:
        decoded = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except (ValueError, TypeError) as error:
        raise ArtifactGenerationError("lock integrity is invalid") from error
    if len(decoded) != 64:
        raise ArtifactGenerationError("lock integrity is invalid")
    return value


def _pnpm_lock_integrities(lock_payload: bytes) -> tuple[str, ...]:
    if not isinstance(lock_payload, bytes) or not lock_payload:
        raise ArtifactGenerationError("pnpm lock is invalid")
    if b"\\" in lock_payload or _FORBIDDEN_PNPM_SOURCE.search(lock_payload):
        raise ArtifactGenerationError("pnpm dependency source is outside the closed registry")
    if _LIFECYCLE_LINE.search(lock_payload) is not None:
        raise ArtifactGenerationError("dependency lifecycle execution is not closed")
    observed: list[str] = []
    for line in lock_payload.splitlines():
        if b"integrity:" not in line:
            continue
        match = _LOCK_INTEGRITY_STANDALONE.fullmatch(line)
        if match is None:
            match = _LOCK_INTEGRITY_INLINE.fullmatch(line)
        if match is None:
            raise ArtifactGenerationError(
                "pnpm lock integrity syntax differs"
            )
        observed.append(_validate_integrity(match.group(1).decode("ascii")))
    if not observed or len(observed) != len(set(observed)):
        raise ArtifactGenerationError("pnpm lock integrity inventory differs")
    return tuple(sorted(observed))


def validate_openclaw_binding(
    *,
    commit: str,
    tree: str,
    version: str,
    package_manager: str,
    node_base_image: str,
    platform: str,
) -> None:
    """Validate the complete audited OpenClaw/toolchain identity."""

    if (
        commit != AUDITED_OPENCLAW_COMMIT
        or tree != AUDITED_OPENCLAW_TREE
        or version != AUDITED_OPENCLAW_VERSION
        or package_manager != OPENCLAW_PACKAGE_MANAGER
        or node_base_image != NODE_BASE_IMAGE
        or platform != PLATFORM
    ):
        raise ArtifactGenerationError("audited OpenClaw binding differs")


def validate_download_hop(url: str) -> str:
    """Accept only one canonical, credential-free npm-registry HTTPS URL."""

    if not isinstance(url, str) or not url.isascii() or len(url) > 2048:
        raise ArtifactGenerationError("dependency URL is invalid")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ArtifactGenerationError("dependency URL is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.netloc != "registry.npmjs.org"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or not parsed.path.endswith(".tgz")
        or _REGISTRY_TARBALL_PATH.fullmatch(parsed.path) is None
        or "//" in parsed.path
        or any(part in {"", ".", ".."} for part in parsed.path.split("/")[1:])
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != url
    ):
        raise ArtifactGenerationError("dependency URL is outside the closed registry")
    return url


@dataclass(frozen=True, slots=True)
class NpmLockRecord:
    path: str
    url: str
    integrity: str


def _bridge_lock_records(
    lock_payload: bytes, *, require_reviewed_digest: bool
) -> tuple[NpmLockRecord, ...]:
    if (
        not isinstance(lock_payload, bytes)
        or not lock_payload
        or (
            require_reviewed_digest
            and _sha256_bytes(lock_payload) != BRIDGE_LOCK_SHA256
        )
    ):
        raise ArtifactGenerationError("reviewed bridge lock differs")
    try:
        lock = json.loads(lock_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactGenerationError("reviewed bridge lock is invalid") from error
    if (
        not isinstance(lock, dict)
        or lock.get("lockfileVersion") != 3
        or not isinstance(lock.get("packages"), dict)
    ):
        raise ArtifactGenerationError("reviewed bridge lock is invalid")
    records: list[NpmLockRecord] = []
    for raw_path, raw_record in sorted(lock["packages"].items()):
        if not isinstance(raw_path, str) or not isinstance(raw_record, dict):
            raise ArtifactGenerationError("reviewed bridge lock inventory differs")
        if set(raw_record).intersection(_LIFECYCLE_KEYS):
            raise ArtifactGenerationError("dependency lifecycle execution is not closed")
        if raw_path == "":
            continue
        url = validate_download_hop(raw_record.get("resolved"))
        integrity = _validate_integrity(raw_record.get("integrity"))
        records.append(NpmLockRecord(raw_path, url, integrity))
    if not records or len({item.path for item in records}) != len(records):
        raise ArtifactGenerationError("reviewed bridge lock inventory differs")
    urls = [item.url for item in records]
    if len(urls) != len(set(urls)):
        raise ArtifactGenerationError("reviewed bridge lock URL inventory differs")
    return tuple(records)


def bridge_lock_records(lock_payload: bytes) -> tuple[NpmLockRecord, ...]:
    """Parse only the exact committed release bridge lock."""

    return _bridge_lock_records(lock_payload, require_reviewed_digest=True)


def sanitized_environment(
    ambient: Mapping[str, str], *, home: Path
) -> dict[str, str]:
    """Create the complete credential-free environment for acquisition tools."""

    del ambient
    path = Path(home)
    if not path.is_absolute() or path.is_symlink():
        raise ArtifactGenerationError("fresh tool home is unsafe")
    return {
        "CI": "true",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "HOME": str(path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_UPDATE_NOTIFIER": "1",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_GLOBALCONFIG": str(path / "global.npmrc"),
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_USERCONFIG": str(path / "user.npmrc"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PNPM_DISABLE_SELF_UPDATE_CHECK": "1",
        "SOURCE_DATE_EPOCH": "0",
        "TZ": "UTC",
    }


def _npm_content_path(cache: Path, integrity: str) -> Path:
    digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True).hex()
    return cache / "_cacache/content-v2/sha512" / digest[:2] / digest[2:4] / digest[4:]


def _npm_index_path(cache: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache / "_cacache/index-v5" / digest[:2] / digest[2:4] / digest[4:]


def _read_npm_index(source: Path) -> dict[str, dict[str, object]]:
    root = source / "_cacache/index-v5"
    if not root.is_dir() or root.is_symlink():
        raise ArtifactGenerationError("npm cache index is missing")
    records: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ArtifactGenerationError("npm cache inventory is unsafe")
        try:
            lines = path.read_bytes().splitlines()
        except OSError as error:
            raise ArtifactGenerationError("npm cache index is unreadable") from error
        for line in lines:
            if not line:
                continue
            try:
                prefix, encoded = line.split(b"\t", 1)
                if prefix.decode("ascii") != hashlib.sha1(encoded).hexdigest():
                    raise ValueError("index checksum")
                value = json.loads(encoded)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ArtifactGenerationError("npm cache index is invalid") from error
            if not isinstance(value, dict) or not isinstance(value.get("key"), str):
                raise ArtifactGenerationError("npm cache index is invalid")
            key = value["key"]
            if _npm_index_path(source, key) != path or key in records:
                raise ArtifactGenerationError("npm cache index inventory differs")
            records[key] = value
    return records


def _copy_regular(source: Path, destination: Path, *, mode: int = 0o644) -> None:
    try:
        metadata = source.lstat()
        if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
            raise ArtifactGenerationError("cache content is unsafe")
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        try:
            with source.open("rb", buffering=0) as reader:
                while True:
                    chunk = reader.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    _write_all(descriptor, chunk)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ArtifactGenerationError("cache content copy failed") from error


def _link_regular(source: Path | None, destination: Path) -> None:
    """Materialize a retained artifact without allocating a second payload."""

    if source is None:
        raise ArtifactGenerationError("retained attempt artifact is missing")
    try:
        metadata = source.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or source.is_symlink()
            or destination.exists()
            or destination.is_symlink()
        ):
            raise ArtifactGenerationError("retained attempt artifact is unsafe")
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        raise ArtifactGenerationError(
            "retained attempt artifact cannot be materialized"
        ) from error


def normalize_npm_cache(
    source: Path, destination: Path, *, lock_payload: bytes
) -> None:
    """Rebuild cacache from exactly the lock URL/integrity set."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_dir() or source.is_symlink():
        raise ArtifactGenerationError("npm cache root is unsafe")
    _mkdir_exclusive(destination)
    try:
        expected = _bridge_lock_records(
            lock_payload, require_reviewed_digest=False
        )
        indexes = _read_npm_index(source)
        expected_content: set[Path] = set()
        expected_index: set[Path] = set()
        for record in expected:
            key = "make-fetch-happen:request-cache:" + record.url
            raw = indexes.get(key)
            if (
                raw is None
                or raw.get("integrity") != record.integrity
                or not isinstance(raw.get("size"), int)
                or raw["size"] < 1
            ):
                raise ArtifactGenerationError("npm cache lock binding differs")
            content = _npm_content_path(source, record.integrity)
            digest, size = _hash_path(content, "sha512")
            expected_digest = base64.b64decode(
                record.integrity.removeprefix("sha512-"), validate=True
            ).hex()
            if digest != expected_digest or size != raw["size"]:
                raise ArtifactGenerationError("npm cache content integrity differs")
            target_content = _npm_content_path(destination, record.integrity)
            _copy_regular(content, target_content)
            expected_content.add(content)
            expected_index.add(_npm_index_path(source, key))
            normalized = {
                "key": key,
                "integrity": record.integrity,
                "time": 1,
                "size": size,
                "metadata": {
                    "time": 1,
                    "url": record.url,
                    "reqHeaders": {},
                    "resHeaders": {"content-type": "application/octet-stream"},
                    "options": {"compress": True},
                },
            }
            encoded = _canonical_json(normalized)
            line = hashlib.sha1(encoded).hexdigest().encode() + b"\t" + encoded + b"\n"
            target_index = _npm_index_path(destination, key)
            target_index.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _write_exclusive(target_index, line, mode=0o644)

        if set(indexes) != {
            "make-fetch-happen:request-cache:" + item.url for item in expected
        }:
            raise ArtifactGenerationError("npm cache index inventory differs")
        observed_content = {
            path
            for path in (source / "_cacache/content-v2/sha512").rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if observed_content != expected_content:
            raise ArtifactGenerationError("npm cache content inventory differs")
        observed_index = {
            path
            for path in (source / "_cacache/index-v5").rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if observed_index != expected_index:
            raise ArtifactGenerationError("npm cache index inventory differs")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def normalize_pnpm_store(store: Path, *, lock_payload: bytes) -> None:
    """Close pnpm's WAL and prove its store is exactly lock-reachable."""

    store = Path(store)
    integrities = set(_pnpm_lock_integrities(lock_payload))
    database = store / "v11/index.db"
    files_root = store / "v11/files"
    if (
        not store.is_dir()
        or store.is_symlink()
        or not database.is_file()
        or database.is_symlink()
        or not files_root.is_dir()
        or files_root.is_symlink()
    ):
        raise ArtifactGenerationError("pnpm store inventory is unsafe")
    try:
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA busy_timeout=0")
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if (
                not isinstance(checkpoint, tuple)
                or len(checkpoint) != 3
                or checkpoint[0] != 0
                or checkpoint[1] != checkpoint[2]
            ):
                raise ArtifactGenerationError("pnpm store WAL checkpoint differs")
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode != ("delete",):
                raise ArtifactGenerationError("pnpm store journal mode differs")
            connection.execute("VACUUM")
            schema = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "package_index" not in schema or not schema.issubset(
                {"package_index", "sqlite_stat1", "sqlite_stat4"}
            ):
                raise ArtifactGenerationError("pnpm store schema differs")
            rows = connection.execute(
                "SELECT key, data FROM package_index ORDER BY key"
            )
            indexed_integrities: set[str] = set()
            referenced: set[str] = set()
            saw_row = False
            for key, payload in rows:
                saw_row = True
                if not isinstance(key, str) or not isinstance(payload, bytes):
                    raise ArtifactGenerationError("pnpm store index differs")
                integrity, separator, package = key.partition("\t")
                if (
                    separator != "\t"
                    or not package
                    or integrity in indexed_integrities
                    or _validate_integrity(integrity) != integrity
                ):
                    raise ArtifactGenerationError("pnpm store index differs")
                indexed_integrities.add(integrity)
                referenced.update(
                    item.decode("ascii")
                    for item in re.findall(rb"[0-9a-f]{128}", payload)
                )
            if not saw_row:
                raise ArtifactGenerationError("pnpm store index is empty")
            if indexed_integrities != integrities or not referenced:
                raise ArtifactGenerationError("pnpm store lock binding differs")
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as error:
        raise ArtifactGenerationError("pnpm store database is invalid") from error

    for sidecar in (
        database.with_name("index.db-wal"),
        database.with_name("index.db-shm"),
    ):
        try:
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise ArtifactGenerationError("pnpm store sidecar is unsafe")
                sidecar.unlink()
        except OSError as error:
            raise ArtifactGenerationError("pnpm store sidecar cleanup failed") from error

    observed: set[str] = set()
    for path in sorted(files_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ArtifactGenerationError("pnpm store inventory is unsafe")
        relative = path.relative_to(files_root)
        if len(relative.parts) != 2 or not re.fullmatch(r"[0-9a-f]{2}", relative.parts[0]) or _STORE_FILE.fullmatch(relative.parts[1]) is None:
            raise ArtifactGenerationError("pnpm store inventory differs")
        identity = relative.parts[0] + relative.parts[1].removesuffix("-exec")
        digest, _ = _hash_path(path, "sha512")
        if digest != identity or identity in observed:
            raise ArtifactGenerationError("pnpm store content integrity differs")
        observed.add(identity)
        path.chmod(0o644)
    if observed != referenced:
        raise ArtifactGenerationError("pnpm store inventory differs")

    allowed = {database}
    allowed.update(
        path for path in files_root.rglob("*") if path.is_file()
    )
    projects = store / "v11/projects"
    if projects.exists() or projects.is_symlink():
        if projects.is_symlink() or not projects.is_dir():
            raise ArtifactGenerationError("pnpm store project cache is unsafe")
        pending = [projects]
        directories: list[Path] = []
        try:
            while pending:
                directory = pending.pop()
                directories.append(directory)
                with os.scandir(directory) as entries:
                    for entry in sorted(entries, key=lambda item: item.name):
                        candidate = Path(entry.path)
                        if entry.is_symlink():
                            candidate.unlink()
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(candidate)
                        else:
                            raise ArtifactGenerationError(
                                "pnpm store project cache is not empty"
                            )
            for directory in reversed(directories):
                directory.rmdir()
        except OSError as error:
            raise ArtifactGenerationError(
                "pnpm store project cache cleanup failed"
            ) from error
    for path in (store / "v11").rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        if path not in allowed:
            raise ArtifactGenerationError("pnpm store inventory differs")
    database.chmod(0o644)


@dataclass(frozen=True, slots=True)
class AttemptArtifact:
    sha256: str
    size: int
    path: Path | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sha256, str)
            or _SHA256.fullmatch(self.sha256) is None
            or not isinstance(self.size, int)
            or not 1 <= self.size <= _MAX_ARTIFACT_BYTES
            or (self.path is not None and not isinstance(self.path, Path))
        ):
            raise ArtifactGenerationError("attempt artifact identity is invalid")

    @classmethod
    def from_path(cls, path: Path) -> "AttemptArtifact":
        retained = Path(path)
        digest, size = _hash_path(retained, "sha256")
        return cls(digest, size, retained)


def _artifact_files(
    *, component: str, cache_root: Path, distribution: Path | None
) -> list[tuple[str, Path]]:
    prefix = {
        "openclaw-runtime": "store",
        "bridge-node-modules": "cache",
    }.get(component)
    if prefix is None:
        raise ArtifactGenerationError("offline artifact component differs")
    root = Path(cache_root)
    if not root.is_dir() or root.is_symlink():
        raise ArtifactGenerationError("offline cache root is unsafe")
    files: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ArtifactGenerationError("offline cache inventory is unsafe")
        relative = path.relative_to(root).as_posix()
        name = f"{prefix}/{relative}"
        _safe_relative(name)
        files.append((name, path))
    if component == "openclaw-runtime":
        if distribution is None:
            raise ArtifactGenerationError("pnpm distribution is missing")
        distribution = Path(distribution)
        digest, size = _hash_path(distribution, "sha512")
        if digest != OPENCLAW_PNPM_DISTRIBUTION_SHA512 or size > _MAX_DISTRIBUTION_BYTES:
            raise ArtifactGenerationError("pnpm distribution differs")
        files.append(("pnpm-distribution.tgz", distribution))
    elif distribution is not None:
        raise ArtifactGenerationError("bridge artifact has a distribution")
    if not files:
        raise ArtifactGenerationError("offline cache inventory is empty")
    return sorted(files)


def _add_streamed_member(
    archive: tarfile.TarFile,
    *,
    name: str,
    source: BinaryIO | None,
    payload: bytes | None,
    size: int,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o644
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.mtime = SOURCE_DATE_EPOCH
    member.pax_headers = {}
    if payload is not None:
        from io import BytesIO

        archive.addfile(member, BytesIO(payload))
    else:
        archive.addfile(member, source)


def write_deterministic_artifact(
    *,
    component: str,
    cache_root: Path,
    lock_payload: bytes,
    output: Path,
    distribution: Path | None = None,
) -> AttemptArtifact:
    """Stream one canonical PAX artifact without retaining cache bytes."""

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ArtifactGenerationError("offline artifact output already exists")
    files = _artifact_files(
        component=component,
        cache_root=cache_root,
        distribution=distribution,
    )
    lock_integrities = (
        list(_pnpm_lock_integrities(lock_payload))
        if component == "openclaw-runtime"
        else sorted(
            {item.integrity for item in _bridge_lock_records(lock_payload, require_reviewed_digest=False)}
        )
    )
    inventory: list[dict[str, object]] = []
    for name, path in files:
        digest, size = _hash_path(path, "sha256")
        inventory.append({"path": name, "sha256": digest, "size": size})
    manifest = _canonical_json(
        {
            "schema": _SCHEMA,
            "component": component,
            "lockSha256": _sha256_bytes(lock_payload),
            "lockIntegrities": lock_integrities,
            "files": inventory,
        }
    )
    if len(manifest) > _MAX_MANIFEST_BYTES:
        raise ArtifactGenerationError("offline artifact manifest is unbounded")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", buffering=0, closefd=False) as target:
            with tarfile.open(
                fileobj=target, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                _add_streamed_member(
                    archive,
                    name="integrity-manifest.json",
                    source=None,
                    payload=manifest,
                    size=len(manifest),
                )
                for name, path in files:
                    with path.open("rb", buffering=0) as source:
                        _add_streamed_member(
                            archive,
                            name=name,
                            source=source,
                            payload=None,
                            size=path.stat().st_size,
                        )
            target.flush()
        os.fsync(descriptor)
    except (OSError, tarfile.TarError) as error:
        try:
            output.unlink()
        except OSError:
            pass
        raise ArtifactGenerationError("offline artifact write failed") from error
    finally:
        os.close(descriptor)
    return AttemptArtifact.from_path(output)


def _member_payload_and_hash(
    archive: tarfile.TarFile, member: tarfile.TarInfo
) -> tuple[bytes | None, str, int]:
    reader = archive.extractfile(member)
    if reader is None:
        raise ArtifactGenerationError("offline artifact member is truncated")
    digest = hashlib.sha256()
    payload = bytearray() if member.name == "integrity-manifest.json" else None
    size = 0
    while True:
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > member.size or (
            payload is not None and size > _MAX_MANIFEST_BYTES
        ):
            raise ArtifactGenerationError("offline artifact member is unbounded")
        digest.update(chunk)
        if payload is not None:
            payload.extend(chunk)
    if size != member.size:
        raise ArtifactGenerationError("offline artifact member is truncated")
    return bytes(payload) if payload is not None else None, digest.hexdigest(), size


def validate_artifact(
    path: Path, *, component: str, lock_payload: bytes
) -> AttemptArtifact:
    """Stream-validate metadata, content inventory, and canonical manifest."""

    if component not in {"openclaw-runtime", "bridge-node-modules"}:
        raise ArtifactGenerationError("offline artifact component differs")
    retained = AttemptArtifact.from_path(Path(path))
    expected_prefix = "store/" if component == "openclaw-runtime" else "cache/"
    inventory: list[dict[str, object]] = []
    manifest_payload: bytes | None = None
    seen: set[str] = set()
    member_order: list[str] = []
    payload_extent = 0
    pnpm_sha512 = hashlib.sha512()
    pnpm_size = 0
    try:
        with tarfile.open(path, mode="r|") as archive:
            for member in archive:
                candidate = _safe_relative(member.name)
                if (
                    not member.isreg()
                    or member.type != tarfile.REGTYPE
                    or member.name in seen
                    or member.mode != 0o644
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname not in {"", None}
                    or member.gname not in {"", None}
                    or member.mtime != SOURCE_DATE_EPOCH
                    or member.linkname not in {"", None}
                    or member.devmajor != 0
                    or member.devminor != 0
                    or member.sparse is not None
                    or set(member.pax_headers).difference({"path"})
                    or (
                        "path" in member.pax_headers
                        and member.pax_headers["path"] != member.name
                    )
                ):
                    raise ArtifactGenerationError("offline artifact metadata differs")
                if (
                    any(part.lower() in _FORBIDDEN_ARCHIVE_PARTS for part in candidate.parts)
                    or member.name.endswith(("-wal", "-shm"))
                ):
                    raise ArtifactGenerationError("offline artifact contains mutable or secret content")
                seen.add(member.name)
                member_order.append(member.name)
                payload, digest, size = _member_payload_and_hash(archive, member)
                if member.name == "integrity-manifest.json":
                    manifest_payload = payload
                    continue
                if component == "openclaw-runtime" and member.name == "pnpm-distribution.tgz":
                    # Hash the distribution in a separate streaming pass below;
                    # its member was already consumed without retention.
                    pass
                elif not member.name.startswith(expected_prefix):
                    raise ArtifactGenerationError("offline artifact inventory differs")
                inventory.append(
                    {"path": member.name, "sha256": digest, "size": size}
                )
            payload_extent = archive.offset
    except (OSError, tarfile.TarError) as error:
        raise ArtifactGenerationError("offline artifact is invalid") from error
    expected_order = ["integrity-manifest.json"] + sorted(
        name for name in seen if name != "integrity-manifest.json"
    )
    if member_order != expected_order:
        raise ArtifactGenerationError("offline artifact member order differs")
    canonical_extent = (
        (
            payload_extent
            + (2 * tarfile.BLOCKSIZE)
            + tarfile.RECORDSIZE
            - 1
        )
        // tarfile.RECORDSIZE
    ) * tarfile.RECORDSIZE
    if retained.size != canonical_extent:
        raise ArtifactGenerationError("offline artifact canonical extent differs")
    if manifest_payload is None or not inventory:
        raise ArtifactGenerationError("offline artifact manifest is missing")
    lock_integrities = (
        list(_pnpm_lock_integrities(lock_payload))
        if component == "openclaw-runtime"
        else sorted(
            {item.integrity for item in _bridge_lock_records(lock_payload, require_reviewed_digest=False)}
        )
    )
    expected = {
        "schema": _SCHEMA,
        "component": component,
        "lockSha256": _sha256_bytes(lock_payload),
        "lockIntegrities": lock_integrities,
        "files": sorted(inventory, key=lambda item: str(item["path"])),
    }
    try:
        observed = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactGenerationError("offline artifact manifest is invalid") from error
    if observed != expected or _canonical_json(observed) != manifest_payload:
        raise ArtifactGenerationError("offline artifact manifest binding differs")
    if component == "openclaw-runtime":
        if "pnpm-distribution.tgz" not in seen or not any(
            name.startswith("store/") for name in seen
        ):
            raise ArtifactGenerationError("offline pnpm artifact inventory differs")
        # A retained-file second pass avoids storing the distribution or the
        # enclosing multi-gigabyte tar in memory.
        try:
            with tarfile.open(path, mode="r|") as archive:
                for member in archive:
                    if member.name != "pnpm-distribution.tgz":
                        continue
                    reader = archive.extractfile(member)
                    if reader is None:
                        break
                    while True:
                        chunk = reader.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        pnpm_size += len(chunk)
                        if pnpm_size > _MAX_DISTRIBUTION_BYTES:
                            raise ArtifactGenerationError("pnpm distribution is unbounded")
                        pnpm_sha512.update(chunk)
                    break
        except (OSError, tarfile.TarError) as error:
            raise ArtifactGenerationError("pnpm distribution is invalid") from error
        if pnpm_sha512.hexdigest() != OPENCLAW_PNPM_DISTRIBUTION_SHA512:
            raise ArtifactGenerationError("pnpm distribution differs")
    elif any(name == "pnpm-distribution.tgz" for name in seen):
        raise ArtifactGenerationError("bridge artifact inventory differs")
    return retained


def assert_attempts_identical(
    first: AttemptArtifact, second: AttemptArtifact
) -> AttemptArtifact:
    if type(first) is not AttemptArtifact or type(second) is not AttemptArtifact:
        raise ArtifactGenerationError("attempt artifact type differs")
    if first.sha256 != second.sha256 or first.size != second.size:
        raise ArtifactGenerationError("attempt artifacts are not byte-identical")
    if first.path is not None and second.path is not None:
        try:
            with first.path.open("rb", buffering=0) as left, second.path.open(
                "rb", buffering=0
            ) as right:
                while True:
                    one = left.read(_COPY_CHUNK)
                    two = right.read(_COPY_CHUNK)
                    if one != two:
                        raise ArtifactGenerationError(
                            "attempt artifacts are not byte-identical"
                        )
                    if not one:
                        break
        except OSError as error:
            raise ArtifactGenerationError("attempt artifact comparison failed") from error
    return first


def canonical_result(
    *, output: Path, openclaw: AttemptArtifact, bridge: AttemptArtifact
) -> bytes:
    if not isinstance(output, Path):
        raise ArtifactGenerationError("result output identity is invalid")
    return _canonical_json(
        {
            "schema": _RESULT_SCHEMA,
            "output": "<caller-provided>",
            "artifacts": {
                "bridge-node-modules": {
                    "sha256": bridge.sha256,
                    "size": bridge.size,
                },
                "openclaw-runtime": {
                    "sha256": openclaw.sha256,
                    "size": openclaw.size,
                },
            },
        }
    )


class _ClosedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_download_hop(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_registry_proxy_request(line: bytes) -> str:
    if line != b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n":
        raise ArtifactGenerationError("registry proxy request is outside the closed origin")
    return _REGISTRY_HOST


def _registry_addresses() -> tuple[tuple[int, int, int, tuple[object, ...]], ...]:
    try:
        records = socket.getaddrinfo(
            _REGISTRY_HOST,
            _REGISTRY_PORT,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as error:
        raise ArtifactGenerationError("registry proxy resolution failed") from error
    closed: list[tuple[int, int, int, tuple[object, ...]]] = []
    for family, socket_type, protocol, _, address in records:
        try:
            candidate = ipaddress.ip_address(str(address[0]).split("%", 1)[0])
        except ValueError as error:
            raise ArtifactGenerationError("registry proxy address is invalid") from error
        if not candidate.is_global:
            raise ArtifactGenerationError("registry proxy address is not public")
        identity = (family, socket_type, protocol, tuple(address))
        if identity not in closed:
            closed.append(identity)
    if not closed:
        raise ArtifactGenerationError("registry proxy address is missing")
    return tuple(closed)


def _open_registry_socket() -> socket.socket:
    for family, socket_type, protocol, address in _registry_addresses():
        connection = socket.socket(family, socket_type, protocol)
        try:
            connection.settimeout(120)
            connection.connect(address)
            connection.settimeout(None)
            return connection
        except OSError:
            connection.close()
    raise ArtifactGenerationError("registry proxy connection failed")


class _RegistryProxyHandler(socketserver.StreamRequestHandler):
    timeout = 130

    def handle(self) -> None:
        try:
            line = self.rfile.readline(4097)
            _validate_registry_proxy_request(line)
            if len(line) > 4096:
                raise ArtifactGenerationError("registry proxy request is unbounded")
            header_bytes = 0
            while True:
                header = self.rfile.readline(4097)
                header_bytes += len(header)
                if len(header) > 4096 or header_bytes > 16384:
                    raise ArtifactGenerationError("registry proxy headers are unbounded")
                if header in {b"\r\n", b"\n"}:
                    break
                if not header:
                    raise ArtifactGenerationError("registry proxy headers are truncated")
            upstream = _open_registry_socket()
        except (ArtifactGenerationError, OSError):
            try:
                self.connection.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            return
        try:
            self.connection.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )
            peers = (self.connection, upstream)
            while True:
                readable, _, _ = select.select(peers, (), (), 120)
                if not readable:
                    return
                for source in readable:
                    payload = source.recv(64 * 1024)
                    if not payload:
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(payload)
        except OSError:
            return
        finally:
            upstream.close()


class _RegistryProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        del request, client_address


@contextmanager
def _registry_proxy() -> Iterator[str]:
    try:
        server = _RegistryProxyServer(("127.0.0.1", 0), _RegistryProxyHandler)
    except OSError as error:
        raise ArtifactGenerationError("registry proxy cannot start") from error
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _proxied_environment(
    environment: Mapping[str, str], *, proxy: str
) -> dict[str, str]:
    if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", proxy):
        raise ArtifactGenerationError("registry proxy identity is invalid")
    value = dict(environment)
    value.update(
        {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "NO_PROXY": "",
            "NPM_CONFIG_HTTPS_PROXY": proxy,
            "NPM_CONFIG_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "no_proxy": "",
        }
    )
    return value


def _download(url: str, destination: Path, *, expected_sha512: str) -> None:
    validate_download_hop(url)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), _ClosedRedirectHandler())
    request = Request(url, headers={"Accept": "application/octet-stream"})
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    digest = hashlib.sha512()
    size = 0
    try:
        with opener.open(request, timeout=120) as response:
            validate_download_hop(response.geturl())
            if response.status != 200:
                raise ArtifactGenerationError("package distribution download failed")
            while True:
                chunk = response.read(_COPY_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_DISTRIBUTION_BYTES:
                    raise ArtifactGenerationError("package distribution is unbounded")
                digest.update(chunk)
                _write_all(descriptor, chunk)
        os.fsync(descriptor)
    except (OSError, HTTPError, URLError) as error:
        raise ArtifactGenerationError("package distribution download failed") from error
    finally:
        os.close(descriptor)
    if size < 1 or digest.hexdigest() != expected_sha512:
        raise ArtifactGenerationError("package distribution digest differs")


@dataclass(frozen=True, slots=True)
class _Completed:
    stdout: bytes


class _Execution:
    __slots__ = ("_environment",)

    def __init__(self, *, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stdout_path: Path | None = None,
        capture: bool = False,
        timeout: int = 7200,
    ) -> _Completed:
        if (
            not command
            or any(not isinstance(item, str) or not item or "\x00" in item for item in command)
            or not Path(cwd).is_absolute()
            or not isinstance(capture, bool)
            or (capture and stdout_path is not None)
        ):
            raise ArtifactGenerationError("acquisition command is unsafe")
        target: int | BinaryIO = subprocess.DEVNULL
        handle: BinaryIO | None = None
        if stdout_path is not None:
            descriptor = os.open(
                stdout_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            handle = os.fdopen(descriptor, "wb", buffering=0)
            target = handle
        elif capture:
            handle = tempfile.TemporaryFile(mode="w+b", buffering=0)
            target = handle
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=target,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
            if completed.returncode != 0:
                raise ArtifactGenerationError("acquisition command failed")
            if stdout_path is not None and handle is not None:
                handle.flush()
                os.fsync(handle.fileno())
            stdout = b""
            if capture and handle is not None:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size > 4096:
                    raise ArtifactGenerationError(
                        "acquisition identity output is unbounded"
                    )
                handle.seek(0)
                stdout = handle.read()
            return _Completed(stdout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArtifactGenerationError("acquisition command failed") from error
        finally:
            if handle is not None:
                handle.close()


def _extract_exact_member(archive_path: Path, name: str, destination: Path) -> None:
    observed = False
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                _safe_relative(member.name)
                if member.name != name:
                    continue
                if observed or not member.isreg() or member.issym() or member.islnk():
                    raise ArtifactGenerationError("package distribution entry differs")
                reader = archive.extractfile(member)
                if reader is None:
                    raise ArtifactGenerationError("package distribution entry is missing")
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    while True:
                        chunk = reader.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        _write_all(descriptor, chunk)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                observed = True
    except (OSError, tarfile.TarError) as error:
        raise ArtifactGenerationError("package distribution is invalid") from error
    if not observed:
        raise ArtifactGenerationError("package distribution entry is missing")


def _extract_distribution_tree(archive_path: Path, destination: Path) -> None:
    """Stream one exact npm distribution into a fresh, link-free tree."""

    _mkdir_exclusive(destination)
    observed = 0
    total = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                candidate = _safe_relative(member.name)
                if candidate.parts[0] != "package":
                    raise ArtifactGenerationError(
                        "package distribution inventory differs"
                    )
                target = destination.joinpath(*candidate.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                if (
                    not member.isreg()
                    or member.issym()
                    or member.islnk()
                    or target.exists()
                    or target.is_symlink()
                ):
                    raise ArtifactGenerationError(
                        "package distribution inventory is unsafe"
                    )
                reader = archive.extractfile(member)
                if reader is None:
                    raise ArtifactGenerationError(
                        "package distribution entry is missing"
                    )
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                descriptor = os.open(
                    target,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o644,
                )
                size = 0
                try:
                    while True:
                        chunk = reader.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if size > member.size or total > 512 * 1024 * 1024:
                            raise ArtifactGenerationError(
                                "package distribution is unbounded"
                            )
                        _write_all(descriptor, chunk)
                    if size != member.size:
                        raise ArtifactGenerationError(
                            "package distribution entry is truncated"
                        )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                observed += 1
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise ArtifactGenerationError("package distribution is invalid") from error
    if observed < 1:
        shutil.rmtree(destination, ignore_errors=True)
        raise ArtifactGenerationError("package distribution is empty")


def _git_value(execution: _Execution, repository: Path, expression: str) -> str:
    raw = execution.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", expression],
        cwd=repository,
        capture=True,
        timeout=120,
    ).stdout
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ArtifactGenerationError("audited Git identity is invalid") from error
    if _GIT_ID.fullmatch(value) is None:
        raise ArtifactGenerationError("audited Git identity is invalid")
    return value


def _copy_bridge_sources(release_repository: Path, destination: Path) -> tuple[bytes, bytes]:
    destination.mkdir(mode=0o700)
    package = release_repository / "bridge/package.json"
    lock = release_repository / "bridge/package-lock.json"
    package_payload = package.read_bytes()
    lock_payload = lock.read_bytes()
    bridge_lock_records(lock_payload)
    try:
        value = json.loads(package_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactGenerationError("bridge package is invalid") from error
    scripts = value.get("scripts", {}) if isinstance(value, dict) else {}
    if not isinstance(scripts, dict) or set(scripts).intersection(_LIFECYCLE_KEYS):
        raise ArtifactGenerationError("dependency lifecycle execution is not closed")
    _write_exclusive(destination / "package.json", package_payload, mode=0o644)
    _write_exclusive(destination / "package-lock.json", lock_payload, mode=0o644)
    return package_payload, lock_payload


def _networkless_proof(
    execution: _Execution,
    *,
    attempt: Path,
) -> None:
    proof = attempt / "networkless-proof"
    proof.mkdir(mode=0o700)
    command = r"""
set -eu
umask 022
mkdir -p /work/openclaw /work/openclaw-pm /work/pnpm /work/bridge /work/bridge-pm /work/home /work/tmp
tar -xf /input/openclaw-source.tar -C /work/openclaw
tar -xf /input/openclaw-runtime.tar -C /work/openclaw-pm
tar -xf /work/openclaw-pm/pnpm-distribution.tgz -C /work/pnpm
cd /work/openclaw
test "$(node /work/pnpm/package/bin/pnpm.cjs --version)" = "11.2.2"
node /work/pnpm/package/bin/pnpm.cjs install --offline --frozen-lockfile --ignore-scripts --store-dir=/work/openclaw-pm/store
node /work/pnpm/package/bin/pnpm.cjs run build
node /work/pnpm/package/bin/pnpm.cjs prune --prod --offline --ignore-scripts --store-dir=/work/openclaw-pm/store
tar -xf /input/bridge-node-modules.tar -C /work/bridge-pm
mkdir -p /work/npm
tar -xf /input/npm-distribution.tgz -C /work/npm
cp /input/bridge-package.json /work/bridge/package.json
cp /input/bridge-package-lock.json /work/bridge/package-lock.json
cd /work/bridge
test "$(node /work/npm/package/bin/npm-cli.js --version)" = "11.12.1"
node /work/npm/package/bin/npm-cli.js ci --offline --ignore-scripts --omit=dev --cache=/work/bridge-pm/cache --audit=false --fund=false --update-notifier=false
test -d node_modules
""".strip()
    execution.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--pull=never",
            "--platform=linux/arm64",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=0:0",
            "--mount",
            f"type=bind,src={attempt},dst=/input,readonly",
            "--mount",
            f"type=bind,src={proof},dst=/work",
            "--env",
            "HOME=/work/home",
            "--env",
            "TMPDIR=/work/tmp",
            "--env",
            "SOURCE_DATE_EPOCH=0",
            NODE_BASE_IMAGE,
            "sh",
            "-euc",
            command,
        ],
        cwd=attempt,
        timeout=14400,
    )
    shutil.rmtree(proof)


def _acquire_attempt(
    *,
    ordinal: int,
    root: Path,
    openclaw_repository: Path,
    release_repository: Path,
) -> tuple[AttemptArtifact, AttemptArtifact]:
    attempt = root / f"attempt-{ordinal}"
    attempt.mkdir(mode=0o700)
    attempt_home = attempt / "home"
    attempt_home.mkdir(mode=0o700)
    environment = sanitized_environment({}, home=attempt_home)
    execution = _Execution(environment=environment)
    pnpm_distribution = attempt / "pnpm-distribution.tgz"
    npm_distribution = attempt / "npm-distribution.tgz"
    _download(
        _PNPM_URL,
        pnpm_distribution,
        expected_sha512=OPENCLAW_PNPM_DISTRIBUTION_SHA512,
    )
    _download(
        _NPM_URL,
        npm_distribution,
        expected_sha512=BRIDGE_NPM_DISTRIBUTION_SHA512,
    )
    openclaw_source = attempt / "openclaw-source.tar"
    execution.run(
        [
            "git",
            "-C",
            str(openclaw_repository),
            "archive",
            "--format=tar",
            AUDITED_OPENCLAW_COMMIT,
        ],
        cwd=openclaw_repository,
        stdout_path=openclaw_source,
        timeout=600,
    )
    source_root = attempt / "openclaw-source"
    source_root.mkdir(mode=0o700)
    execution.run(
        ["tar", "-xf", str(openclaw_source), "-C", str(source_root)],
        cwd=attempt,
        timeout=600,
    )
    package = json.loads((source_root / "package.json").read_bytes())
    validate_openclaw_binding(
        commit=AUDITED_OPENCLAW_COMMIT,
        tree=AUDITED_OPENCLAW_TREE,
        version=package.get("version"),
        package_manager=package.get("packageManager"),
        node_base_image=NODE_BASE_IMAGE,
        platform=PLATFORM,
    )
    openclaw_lock = (source_root / "pnpm-lock.yaml").read_bytes()
    _pnpm_lock_integrities(openclaw_lock)
    pnpm_cli = attempt / "pnpm.cjs"
    _extract_exact_member(
        pnpm_distribution, "package/bin/pnpm.cjs", pnpm_cli
    )
    pnpm_store = attempt / "pnpm-store"
    with _registry_proxy() as registry_proxy:
        _Execution(
            environment=_proxied_environment(
                environment,
                proxy=registry_proxy,
            )
        ).run(
            [
                "node",
                str(pnpm_cli),
                "fetch",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--ignore-pnpmfile",
                "--registry=https://registry.npmjs.org/",
                f"--https-proxy={registry_proxy}",
                f"--proxy={registry_proxy}",
                f"--store-dir={pnpm_store}",
            ],
            cwd=source_root,
            timeout=7200,
        )
    normalize_pnpm_store(pnpm_store, lock_payload=openclaw_lock)

    bridge_source = attempt / "bridge-source"
    _, bridge_lock = _copy_bridge_sources(release_repository, bridge_source)
    npm_tree = attempt / "npm"
    _extract_distribution_tree(npm_distribution, npm_tree)
    npm_cli = npm_tree / "package/bin/npm-cli.js"
    try:
        npm_package = json.loads(
            (npm_tree / "package/package.json").read_bytes()
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactGenerationError("npm distribution package is invalid") from error
    if not isinstance(npm_package, dict) or npm_package.get("version") != BRIDGE_NPM_VERSION:
        raise ArtifactGenerationError("npm distribution version differs")
    raw_cache = attempt / "npm-cache-raw"
    with _registry_proxy() as registry_proxy:
        _Execution(
            environment=_proxied_environment(
                environment,
                proxy=registry_proxy,
            )
        ).run(
            [
                "node",
                str(npm_cli),
                "ci",
                "--ignore-scripts",
                "--omit=dev",
                f"--cache={raw_cache}",
                "--loglevel=silent",
                "--audit=false",
                "--fund=false",
                "--update-notifier=false",
                "--registry=https://registry.npmjs.org/",
                f"--https-proxy={registry_proxy}",
                f"--proxy={registry_proxy}",
            ],
            cwd=bridge_source,
            timeout=3600,
        )
    normalized_cache = attempt / "npm-cache"
    normalize_npm_cache(
        raw_cache, normalized_cache, lock_payload=bridge_lock
    )

    openclaw_artifact_path = attempt / "openclaw-runtime.tar"
    bridge_artifact_path = attempt / "bridge-node-modules.tar"
    openclaw_artifact = write_deterministic_artifact(
        component="openclaw-runtime",
        cache_root=pnpm_store,
        lock_payload=openclaw_lock,
        output=openclaw_artifact_path,
        distribution=pnpm_distribution,
    )
    bridge_artifact = write_deterministic_artifact(
        component="bridge-node-modules",
        cache_root=normalized_cache,
        lock_payload=bridge_lock,
        output=bridge_artifact_path,
    )
    validate_artifact(
        openclaw_artifact_path,
        component="openclaw-runtime",
        lock_payload=openclaw_lock,
    )
    validate_artifact(
        bridge_artifact_path,
        component="bridge-node-modules",
        lock_payload=bridge_lock,
    )
    _copy_regular(
        bridge_source / "package.json",
        attempt / "bridge-package.json",
    )
    _copy_regular(
        bridge_source / "package-lock.json",
        attempt / "bridge-package-lock.json",
    )
    _networkless_proof(
        execution,
        attempt=attempt,
    )
    return openclaw_artifact, bridge_artifact


def _artifact_set_manifest(
    *, openclaw: AttemptArtifact, bridge: AttemptArtifact
) -> bytes:
    return _canonical_json(
        {
            "schema": _SET_SCHEMA,
            "platform": PLATFORM,
            "nodeBaseImage": NODE_BASE_IMAGE,
            "sourceDateEpoch": SOURCE_DATE_EPOCH,
            "openclaw": {
                "commit": AUDITED_OPENCLAW_COMMIT,
                "tree": AUDITED_OPENCLAW_TREE,
                "version": AUDITED_OPENCLAW_VERSION,
                "packageManager": OPENCLAW_PACKAGE_MANAGER,
                "artifactSha256": openclaw.sha256,
                "artifactSize": openclaw.size,
                "attemptDigests": [openclaw.sha256, openclaw.sha256],
            },
            "bridge": {
                "lockSha256": BRIDGE_LOCK_SHA256,
                "packageManager": f"npm@{BRIDGE_NPM_VERSION}",
                "distributionSha512": BRIDGE_NPM_DISTRIBUTION_SHA512,
                "artifactSha256": bridge.sha256,
                "artifactSize": bridge.size,
                "attemptDigests": [bridge.sha256, bridge.sha256],
            },
            "proof": {
                "networkMode": "none",
                "attempts": 2,
                "pnpm": ["install", "build", "prune"],
                "npm": ["ci", "--offline"],
            },
        }
    )


def prepare_offline_dependency_artifacts(
    *,
    openclaw_repository: Path,
    release_repository: Path,
    output: Path,
) -> tuple[AttemptArtifact, AttemptArtifact]:
    """Acquire twice and retain only byte-identical, offline-proven artifacts."""

    if os.environ.get(_INTEGRATION_GATE) != "1":
        raise ArtifactGenerationError(
            "real offline dependency acquisition is not explicitly enabled"
        )
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ArtifactGenerationError("fresh output path already exists")
    if host_platform.system() != "Linux" or host_platform.machine() not in {
        "aarch64",
        "arm64",
    }:
        raise ArtifactGenerationError("acquisition platform differs")
    openclaw_repository = Path(openclaw_repository).resolve()
    release_repository = Path(release_repository).resolve()
    for repository in (openclaw_repository, release_repository):
        if not repository.is_dir() or repository.is_symlink():
            raise ArtifactGenerationError("acquisition repository is unsafe")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    try:
        temporary.chmod(0o700)
        home = temporary / "home"
        home.mkdir(mode=0o700)
        execution = _Execution(
            environment=sanitized_environment(os.environ, home=home)
        )
        raw_node_version = execution.run(
            ["node", "--version"], cwd=temporary, capture=True
        ).stdout.strip()
        try:
            node_version = raw_node_version.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise ArtifactGenerationError("Node version differs") from error
        if node_version != NODE_VERSION:
            raise ArtifactGenerationError("Node version differs")
        if _git_value(
            execution,
            openclaw_repository,
            f"{AUDITED_OPENCLAW_COMMIT}^{{commit}}",
        ) != AUDITED_OPENCLAW_COMMIT or _git_value(
            execution,
            openclaw_repository,
            f"{AUDITED_OPENCLAW_COMMIT}^{{tree}}",
        ) != AUDITED_OPENCLAW_TREE:
            raise ArtifactGenerationError("audited OpenClaw Git binding differs")
        bridge_lock_records(
            (release_repository / "bridge/package-lock.json").read_bytes()
        )
        attempts = [
            _acquire_attempt(
                ordinal=ordinal,
                root=temporary,
                openclaw_repository=openclaw_repository,
                release_repository=release_repository,
            )
            for ordinal in (1, 2)
        ]
        openclaw = assert_attempts_identical(attempts[0][0], attempts[1][0])
        bridge = assert_attempts_identical(attempts[0][1], attempts[1][1])
        final = temporary / "final"
        final.mkdir(mode=0o700)
        _link_regular(openclaw.path, final / "openclaw-runtime.tar")
        _link_regular(bridge.path, final / "bridge-node-modules.tar")
        manifest = _artifact_set_manifest(
            openclaw=openclaw,
            bridge=bridge,
        )
        _write_exclusive(final / "manifest.json", manifest, mode=0o600)
        final_descriptor = os.open(final, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(final_descriptor)
        finally:
            os.close(final_descriptor)
        os.rename(final, output)
        parent_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        retained_openclaw = AttemptArtifact(
            openclaw.sha256, openclaw.size, output / "openclaw-runtime.tar"
        )
        retained_bridge = AttemptArtifact(
            bridge.sha256, bridge.size, output / "bridge-node-modules.tar"
        )
        return retained_openclaw, retained_bridge
    except Exception:
        if output.exists() and output.is_dir():
            shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


__all__ = [
    "AUDITED_OPENCLAW_COMMIT",
    "AUDITED_OPENCLAW_TREE",
    "AUDITED_OPENCLAW_VERSION",
    "BRIDGE_LOCK_SHA256",
    "BRIDGE_NPM_DISTRIBUTION_SHA512",
    "NODE_BASE_IMAGE",
    "OPENCLAW_PNPM_DISTRIBUTION_SHA512",
    "ArtifactGenerationError",
    "AttemptArtifact",
    "assert_attempts_identical",
    "bridge_lock_records",
    "canonical_result",
    "normalize_npm_cache",
    "normalize_pnpm_store",
    "prepare_offline_dependency_artifacts",
    "sanitized_environment",
    "validate_artifact",
    "validate_download_hop",
    "validate_openclaw_binding",
    "write_deterministic_artifact",
]
