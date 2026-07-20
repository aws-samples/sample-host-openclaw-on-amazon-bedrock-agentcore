from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from release_tools.offline_dependency_artifacts_v2 import (
    AUDITED_OPENCLAW_COMMIT,
    AUDITED_OPENCLAW_TREE,
    AUDITED_OPENCLAW_VERSION,
    BRIDGE_LOCK_SHA256,
    BRIDGE_NPM_DISTRIBUTION_SHA512,
    NODE_BASE_IMAGE,
    OPENCLAW_PNPM_DISTRIBUTION_SHA512,
    ArtifactGenerationError,
    AttemptArtifact,
    assert_attempts_identical,
    bridge_lock_records,
    canonical_result,
    normalize_npm_cache,
    normalize_pnpm_store,
    prepare_offline_dependency_artifacts,
    sanitized_environment,
    validate_artifact,
    validate_download_hop,
    validate_openclaw_binding,
    write_deterministic_artifact,
)
from release_tools import offline_dependency_artifacts_v2 as artifacts_v2


def _sha512_integrity(payload: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode(
        "ascii"
    )


def _openclaw_package(**changes: object) -> bytes:
    value: dict[str, object] = {
        "name": "openclaw",
        "version": AUDITED_OPENCLAW_VERSION,
        "packageManager": (
            "pnpm@11.2.2+sha512."
            + OPENCLAW_PNPM_DISTRIBUTION_SHA512
        ),
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _bridge_lock(url: str, integrity: str) -> bytes:
    return json.dumps(
        {
            "name": "openclaw-bridge",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "openclaw-bridge",
                    "version": "1.0.0",
                },
                "node_modules/example": {
                    "version": "1.2.3",
                    "resolved": url,
                    "integrity": integrity,
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _write_cache_entry(
    cache: Path,
    *,
    url: str,
    payload: bytes,
    integrity: str,
    extra: dict[str, object] | None = None,
) -> None:
    digest = hashlib.sha512(payload).hexdigest()
    content = cache / "_cacache/content-v2/sha512" / digest[:2] / digest[2:4]
    content.mkdir(parents=True, exist_ok=True)
    (content / digest[4:]).write_bytes(payload)
    key = "make-fetch-happen:request-cache:" + url
    record: dict[str, object] = {
        "key": key,
        "integrity": integrity,
        "time": 987654321,
        "size": len(payload),
        "metadata": {
            "time": 987654321,
            "url": url,
            "reqHeaders": {"authorization": "must-not-survive"},
            "resHeaders": {
                "content-type": "application/octet-stream",
                "x-request-id": "mutable",
            },
            "options": {"compress": True},
        },
    }
    if extra:
        record.update(extra)
    encoded = json.dumps(record, separators=(",", ":")).encode()
    key_digest = hashlib.sha256(key.encode()).hexdigest()
    index = cache / "_cacache/index-v5" / key_digest[:2] / key_digest[2:4]
    index.mkdir(parents=True, exist_ok=True)
    (index / key_digest[4:]).write_bytes(
        hashlib.sha1(encoded).hexdigest().encode() + b"\t" + encoded + b"\n"
    )


def _pnpm_lock(*integrities: str, lifecycle: str = "") -> bytes:
    rows = ["lockfileVersion: '9.0'", "", "packages:"]
    for ordinal, integrity in enumerate(integrities):
        rows.extend(
            [
                f"  example@{ordinal + 1}.0.0:",
                "    resolution:",
                f"      integrity: {integrity}",
            ]
        )
        if lifecycle:
            rows.append(f"    {lifecycle}: true")
    return ("\n".join(rows) + "\n").encode()


def _make_pnpm_store(
    root: Path, *, package_integrity: str, files: dict[str, bytes]
) -> Path:
    store = root / "store"
    database = store / "v11/index.db"
    database.parent.mkdir(parents=True)
    referenced: list[str] = []
    for payload in files.values():
        digest = hashlib.sha512(payload).hexdigest()
        target = store / "v11/files" / digest[:2] / digest[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        referenced.append(digest)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE package_index (key TEXT PRIMARY KEY, data BLOB NOT NULL) "
        "WITHOUT ROWID"
    )
    body = ("|".join(referenced)).encode()
    connection.execute(
        "INSERT INTO package_index(key, data) VALUES (?, ?)",
        (package_integrity + "\texample@1.0.0", body),
    )
    connection.commit()
    connection.close()
    # A closed WAL database can leave either sidecar. The normalizer must remove
    # both only after a successful checkpoint and journal-mode transition.
    (database.parent / "index.db-shm").touch(exist_ok=True)
    (database.parent / "index.db-wal").touch(exist_ok=True)
    return store


def _tar_payload(entries: list[tuple[str, bytes, int, int]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload, mode, mtime in entries:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            member.uid = member.gid = 0
            member.mtime = mtime
            tar.addfile(member, io.BytesIO(payload))
    return output.getvalue()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "0" * 40),
        ("tree", "1" * 40),
        ("version", "2026.7.3"),
        ("platform", "linux/amd64"),
        ("node_base_image", NODE_BASE_IMAGE.replace("4e6b", "ffff")),
        (
            "package_manager",
            "pnpm@11.2.1+sha512." + OPENCLAW_PNPM_DISTRIBUTION_SHA512,
        ),
        (
            "package_manager",
            "pnpm@11.2.2+sha512." + "0" * 128,
        ),
    ],
)
def test_openclaw_binding_rejects_identity_drift(field: str, value: str) -> None:
    arguments = {
        "commit": AUDITED_OPENCLAW_COMMIT,
        "tree": AUDITED_OPENCLAW_TREE,
        "version": AUDITED_OPENCLAW_VERSION,
        "platform": "linux/arm64",
        "node_base_image": NODE_BASE_IMAGE,
        "package_manager": (
            "pnpm@11.2.2+sha512."
            + OPENCLAW_PNPM_DISTRIBUTION_SHA512
        ),
    }
    arguments[field] = value

    with pytest.raises(ArtifactGenerationError, match="binding differs"):
        validate_openclaw_binding(**arguments)


def test_bridge_lock_requires_exact_bound_digest() -> None:
    with pytest.raises(ArtifactGenerationError, match="bridge lock"):
        bridge_lock_records(b"{}")

    assert len(BRIDGE_LOCK_SHA256) == 64


def test_committed_bridge_lock_has_one_closed_record_per_dependency() -> None:
    lock = Path(__file__).parents[1] / "bridge/package-lock.json"

    records = bridge_lock_records(lock.read_bytes())

    assert len(records) == 37
    assert len({record.url for record in records}) == 37
    assert all(record.url.startswith("https://registry.npmjs.org/") for record in records)


def test_bridge_lock_rejects_lifecycle_execution_even_with_valid_records() -> None:
    payload = b"package"
    lock = json.loads(
        _bridge_lock(
            "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
            _sha512_integrity(payload),
        )
    )
    lock["packages"]["node_modules/example"]["postinstall"] = "node setup.js"
    encoded = json.dumps(lock, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(ArtifactGenerationError, match="lifecycle"):
        artifacts_v2._bridge_lock_records(
            encoded, require_reviewed_digest=False
        )


def test_bridge_lock_rejects_npm_install_script_marker() -> None:
    lock = json.loads(
        _bridge_lock(
            "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
            _sha512_integrity(b"package"),
        )
    )
    lock["packages"]["node_modules/example"]["hasInstallScript"] = True

    with pytest.raises(ArtifactGenerationError, match="lifecycle"):
        artifacts_v2._bridge_lock_records(
            json.dumps(lock).encode(), require_reviewed_digest=False
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.npmjs.org/example/-/example-1.2.3.tgz",
        "https://evil.invalid/example.tgz",
        "https://registry.npmjs.org.evil.invalid/example.tgz",
        "https://user@registry.npmjs.org/example.tgz",
        "https://registry.npmjs.org:444/example.tgz",
        "https://registry.npmjs.org/%2e%2e/example.tgz",
        "https://registry.npmjs.org/example%2ftoken.tgz",
        "https://registry.npmjs.org/example\\token.tgz",
        "https://registry.npmjs.org/example.tgz?token=secret",
        "https://registry.npmjs.org/example.tgz#fragment",
    ],
)
def test_dependency_and_redirect_urls_are_closed(url: str) -> None:
    with pytest.raises(ArtifactGenerationError, match="URL"):
        validate_download_hop(url)


@pytest.mark.parametrize(
    "line",
    [
        b"CONNECT evil.invalid:443 HTTP/1.1\r\n",
        b"CONNECT registry.npmjs.org:80 HTTP/1.1\r\n",
        b"CONNECT user@registry.npmjs.org:443 HTTP/1.1\r\n",
        b"GET https://registry.npmjs.org/x HTTP/1.1\r\n",
        b"CONNECT registry.npmjs.org:443 HTTP/1.0\r\n",
    ],
)
def test_registry_proxy_allows_only_exact_npm_tls_authority(line: bytes) -> None:
    with pytest.raises(ArtifactGenerationError, match="proxy"):
        artifacts_v2._validate_registry_proxy_request(line)

    assert artifacts_v2._validate_registry_proxy_request(
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n"
    ) == "registry.npmjs.org"


def test_npm_cache_is_rebuilt_from_exact_lock_records(tmp_path: Path) -> None:
    package = b"package tar bytes"
    integrity = _sha512_integrity(package)
    url = "https://registry.npmjs.org/example/-/example-1.2.3.tgz"
    lock = _bridge_lock(url, integrity)
    source = tmp_path / "source"
    destination = tmp_path / "normalized"
    _write_cache_entry(
        source,
        url=url,
        payload=package,
        integrity=integrity,
    )

    normalize_npm_cache(source, destination, lock_payload=lock)

    files = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert all("_logs" not in path for path in files)
    index = next((destination / "_cacache/index-v5").rglob("*"))
    while index.is_dir():
        index = next(index.iterdir())
    record = json.loads(index.read_bytes().split(b"\t", 1)[1])
    assert record["time"] == 1
    assert record["metadata"] == {
        "time": 1,
        "url": url,
        "reqHeaders": {},
        "resHeaders": {"content-type": "application/octet-stream"},
        "options": {"compress": True},
    }
    assert b"authorization" not in index.read_bytes()


def test_npm_cache_rejects_lock_url_integrity_and_mutable_cache_injection(
    tmp_path: Path,
) -> None:
    payload = b"package"
    integrity = _sha512_integrity(payload)
    url = "https://registry.npmjs.org/example/-/example-1.2.3.tgz"
    source = tmp_path / "source"
    _write_cache_entry(
        source,
        url=url,
        payload=payload,
        integrity=integrity,
    )
    injected = source / "_cacache/content-v2/sha512/aa/bb"
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_bytes(b"ambient mutable cache")

    with pytest.raises(ArtifactGenerationError, match="inventory"):
        normalize_npm_cache(
            source,
            tmp_path / "out",
            lock_payload=_bridge_lock(url, integrity),
        )


def test_pnpm_store_is_closed_checkpointed_and_vacuumed(tmp_path: Path) -> None:
    package_integrity = _sha512_integrity(b"package archive")
    lock = _pnpm_lock(package_integrity)
    store = _make_pnpm_store(
        tmp_path,
        package_integrity=package_integrity,
        files={"index.js": b"export default 1;\n"},
    )
    projects = store / "v11/projects"
    (projects / "empty-project-id").mkdir(parents=True)
    (projects / "linked-project-id").symlink_to(tmp_path, target_is_directory=True)

    normalize_pnpm_store(store, lock_payload=lock)

    database = store / "v11/index.db"
    assert not (store / "v11/projects").exists()
    assert not database.with_name("index.db-wal").exists()
    assert not database.with_name("index.db-shm").exists()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == (
            "delete",
        )
        assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)


def test_pnpm_normalizer_streams_index_rows_instead_of_materializing_them() -> None:
    source = inspect.getsource(normalize_pnpm_store)

    assert ".fetchall(" not in source


def test_pnpm_lock_accepts_the_audited_inline_resolution_shape() -> None:
    integrity = _sha512_integrity(b"package archive")
    lock = (
        "lockfileVersion: '9.0'\n\npackages:\n\n"
        "  example@1.0.0:\n"
        f"    resolution: {{integrity: {integrity}}}\n"
    ).encode()

    assert artifacts_v2._pnpm_lock_integrities(lock) == (integrity,)


@pytest.mark.parametrize(
    "source",
    [
        "tarball: https://evil.invalid/example.tgz",
        "resolved: http://registry.npmjs.org/example.tgz",
        "repository: git+https://evil.invalid/example.git",
        "directory: file:../../../../outside",
    ],
)
def test_pnpm_lock_rejects_non_registry_or_non_tarball_sources(
    source: str,
) -> None:
    integrity = _sha512_integrity(b"package archive")
    lock = _pnpm_lock(integrity) + f"    {source}\n".encode()

    with pytest.raises(ArtifactGenerationError, match="source"):
        artifacts_v2._pnpm_lock_integrities(lock)


def test_pnpm_store_rejects_lifecycle_and_unreferenced_cache_files(
    tmp_path: Path,
) -> None:
    package_integrity = _sha512_integrity(b"package archive")
    store = _make_pnpm_store(
        tmp_path,
        package_integrity=package_integrity,
        files={"index.js": b"export default 1;\n"},
    )
    extra = b"injected"
    digest = hashlib.sha512(extra).hexdigest()
    path = store / "v11/files" / digest[:2] / digest[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(extra)

    with pytest.raises(ArtifactGenerationError, match="store inventory"):
        normalize_pnpm_store(
            store,
            lock_payload=_pnpm_lock(package_integrity),
        )

    with pytest.raises(ArtifactGenerationError, match="lifecycle"):
        normalize_pnpm_store(
            store,
            lock_payload=_pnpm_lock(
                package_integrity, lifecycle="requiresBuild"
            ),
        )


def test_deterministic_pax_tar_streams_long_paths_and_round_trips(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    long_path = cache / ("long-" + "x" * 180) / "package.tgz"
    long_path.parent.mkdir(parents=True)
    long_path.write_bytes(b"payload")
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"

    write_deterministic_artifact(
        component="bridge-node-modules",
        cache_root=cache,
        lock_payload=lock,
        output=first,
    )
    write_deterministic_artifact(
        component="bridge-node-modules",
        cache_root=cache,
        lock_payload=lock,
        output=second,
    )

    assert first.read_bytes() == second.read_bytes()
    validated = validate_artifact(
        first,
        component="bridge-node-modules",
        lock_payload=lock,
    )
    assert validated.sha256 == hashlib.sha256(first.read_bytes()).hexdigest()
    assert validated.size == first.stat().st_size


def test_bridge_artifact_is_accepted_by_existing_runtime_closure_contract(
    tmp_path: Path,
) -> None:
    from release_tools.image_publication import (
        PackageManagerArtifact,
        _offline_artifact_contract,
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "content").write_bytes(b"package")
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    output = tmp_path / "bridge.tar"
    identity = write_deterministic_artifact(
        component="bridge-node-modules",
        cache_root=cache,
        lock_payload=lock,
        output=output,
    )
    payload = output.read_bytes()

    contract = _offline_artifact_contract(
        component="bridge-node-modules",
        artifact=PackageManagerArtifact(
            "npm", "11.12.1", payload, identity.sha256, ""
        ),
        lock_payload=lock,
    )

    assert len(contract) == 64


def test_artifact_writer_never_materializes_cache_or_tar_payload_in_memory() -> None:
    source = inspect.getsource(write_deterministic_artifact)

    assert ".read_bytes(" not in source
    assert "tarfile.open" in source
    assert "fileobj=target" in source


@pytest.mark.parametrize("name", ["back\\slash", "unicode-é", "line\nbreak"])
def test_artifact_writer_rejects_noncanonical_cache_paths(
    tmp_path: Path, name: str
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / name).write_bytes(b"payload")
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )

    with pytest.raises(ArtifactGenerationError, match="path"):
        write_deterministic_artifact(
            component="bridge-node-modules",
            cache_root=cache,
            lock_payload=lock,
            output=tmp_path / "artifact.tar",
        )


def test_artifact_rejects_metadata_manifest_wal_and_log_drift(
    tmp_path: Path,
) -> None:
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    file_manifest = {
        "path": "cache/value",
        "sha256": hashlib.sha256(b"value").hexdigest(),
        "size": 5,
    }
    manifest = json.dumps(
        {
            "schema": "personal-operator.offline-dependency-cache.v1",
            "component": "bridge-node-modules",
            "lockSha256": hashlib.sha256(lock).hexdigest(),
            "lockIntegrities": [integrity],
            "files": [file_manifest],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    cases = {
        "metadata": [
            ("cache/value", b"value", 0o600, 0),
            ("integrity-manifest.json", manifest, 0o644, 0),
        ],
        "manifest": [
            ("cache/value", b"changed", 0o644, 0),
            ("integrity-manifest.json", manifest, 0o644, 0),
        ],
        "WAL": [
            ("cache/index.db-wal", b"wal", 0o644, 0),
            ("integrity-manifest.json", manifest, 0o644, 0),
        ],
        "log": [
            ("cache/_logs/secret.log", b"token=secret", 0o644, 0),
            ("integrity-manifest.json", manifest, 0o644, 0),
        ],
    }
    for label, entries in cases.items():
        path = tmp_path / f"{label}.tar"
        path.write_bytes(_tar_payload(entries))
        with pytest.raises(ArtifactGenerationError):
            validate_artifact(
                path,
                component="bridge-node-modules",
                lock_payload=lock,
            )


def test_artifact_validator_rejects_noncanonical_member_order(
    tmp_path: Path,
) -> None:
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    files = [
        {
            "path": "cache/a",
            "sha256": hashlib.sha256(b"a").hexdigest(),
            "size": 1,
        },
        {
            "path": "cache/z",
            "sha256": hashlib.sha256(b"z").hexdigest(),
            "size": 1,
        },
    ]
    manifest = json.dumps(
        {
            "schema": "personal-operator.offline-dependency-cache.v1",
            "component": "bridge-node-modules",
            "lockSha256": hashlib.sha256(lock).hexdigest(),
            "lockIntegrities": [integrity],
            "files": files,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    artifact = tmp_path / "unordered.tar"
    artifact.write_bytes(
        _tar_payload(
            [
                ("integrity-manifest.json", manifest, 0o644, 0),
                ("cache/z", b"z", 0o644, 0),
                ("cache/a", b"a", 0o644, 0),
            ]
        )
    )

    with pytest.raises(ArtifactGenerationError, match="order"):
        validate_artifact(
            artifact,
            component="bridge-node-modules",
            lock_payload=lock,
        )


@pytest.mark.parametrize("trailer", [b"SECRET-TRAILER", b"\0" * 10240])
def test_artifact_validator_rejects_bytes_after_canonical_tar_extent(
    tmp_path: Path, trailer: bytes
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "value").write_bytes(b"value")
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    artifact = tmp_path / "artifact.tar"
    write_deterministic_artifact(
        component="bridge-node-modules",
        cache_root=cache,
        lock_payload=lock,
        output=artifact,
    )
    with artifact.open("ab", buffering=0) as handle:
        handle.write(trailer)

    with pytest.raises(ArtifactGenerationError, match="extent"):
        validate_artifact(
            artifact,
            component="bridge-node-modules",
            lock_payload=lock,
        )


def test_artifact_validator_rejects_unused_regular_member_linkname(
    tmp_path: Path,
) -> None:
    integrity = _sha512_integrity(b"package archive")
    lock = _bridge_lock(
        "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        integrity,
    )
    manifest = json.dumps(
        {
            "schema": "personal-operator.offline-dependency-cache.v1",
            "component": "bridge-node-modules",
            "lockSha256": hashlib.sha256(lock).hexdigest(),
            "lockIntegrities": [integrity],
            "files": [
                {
                    "path": "cache/value",
                    "sha256": hashlib.sha256(b"value").hexdigest(),
                    "size": 5,
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in (
            ("integrity-manifest.json", manifest),
            ("cache/value", b"value"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.mtime = 0
            if name == "cache/value":
                member.linkname = "SECRET-LINK-METADATA"
            archive.addfile(member, io.BytesIO(payload))
    artifact = tmp_path / "linkname.tar"
    artifact.write_bytes(output.getvalue())

    with pytest.raises(ArtifactGenerationError, match="metadata"):
        validate_artifact(
            artifact,
            component="bridge-node-modules",
            lock_payload=lock,
        )


def test_attempt_artifacts_must_be_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    attempts = (
        AttemptArtifact.from_path(first),
        AttemptArtifact.from_path(second),
    )

    with pytest.raises(ArtifactGenerationError, match="attempt artifacts"):
        assert_attempts_identical(*attempts)


def test_attempt_comparison_checks_bytes_even_for_equal_claimed_digests(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    with pytest.raises(ArtifactGenerationError, match="byte-identical"):
        assert_attempts_identical(
            AttemptArtifact("a" * 64, 3, first),
            AttemptArtifact("a" * 64, 3, second),
        )


def test_environment_and_result_do_not_expose_credentials_or_source_paths(
    tmp_path: Path,
) -> None:
    environment = sanitized_environment(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "NPM_TOKEN": "secret",
            "NODE_AUTH_TOKEN": "secret",
            "HOME": "/Users/private",
        },
        home=tmp_path / "fresh-home",
    )
    assert set(environment) == {
        "CI",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_UPDATE_NOTIFIER",
        "NPM_CONFIG_AUDIT",
        "NPM_CONFIG_FUND",
        "NPM_CONFIG_GLOBALCONFIG",
        "NPM_CONFIG_REGISTRY",
        "NPM_CONFIG_UPDATE_NOTIFIER",
        "NPM_CONFIG_USERCONFIG",
        "PATH",
        "PNPM_DISABLE_SELF_UPDATE_CHECK",
        "SOURCE_DATE_EPOCH",
        "TZ",
    }
    assert environment["NPM_CONFIG_GLOBALCONFIG"] == str(
        tmp_path / "fresh-home/global.npmrc"
    )
    assert environment["NPM_CONFIG_USERCONFIG"] == str(
        tmp_path / "fresh-home/user.npmrc"
    )
    assert environment["NPM_CONFIG_GLOBALCONFIG"] != environment["NPM_CONFIG_USERCONFIG"]
    assert environment["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
    assert "secret" not in repr(environment)
    result = canonical_result(
        output=Path("secret-project-artifacts"),
        openclaw=AttemptArtifact("a" * 64, 10),
        bridge=AttemptArtifact("b" * 64, 20),
    )
    assert b"secret" not in result
    assert b"/Users/private" not in result
    assert json.loads(result)["output"] == "<caller-provided>"


def test_execution_discards_tool_logs_and_bounds_identity_capture(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    execution = artifacts_v2._Execution(
        environment=sanitized_environment({}, home=home)
    )

    discarded = execution.run(
        ["sh", "-c", "printf credential-like-tool-log"],
        cwd=tmp_path,
    )
    captured = execution.run(
        ["sh", "-c", "printf exact-identity"],
        cwd=tmp_path,
        capture=True,
    )

    assert discarded.stdout == b""
    assert captured.stdout == b"exact-identity"
    with pytest.raises(ArtifactGenerationError, match="output is unbounded"):
        execution.run(
            ["sh", "-c", "head -c 5000 /dev/zero"],
            cwd=tmp_path,
            capture=True,
        )


def test_git_identity_decode_fails_closed_without_unicode_escape(
    tmp_path: Path,
) -> None:
    class InvalidIdentityExecution:
        def run(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return artifacts_v2._Completed(b"\xffsecret")

    with pytest.raises(ArtifactGenerationError, match="Git identity"):
        artifacts_v2._git_value(
            InvalidIdentityExecution(),  # type: ignore[arg-type]
            tmp_path,
            "HEAD^{commit}",
        )


def test_each_attempt_owns_fresh_distribution_acquisition_and_full_npm_tree() -> None:
    signature = inspect.signature(artifacts_v2._acquire_attempt)
    source = inspect.getsource(artifacts_v2._acquire_attempt)
    proof = inspect.getsource(artifacts_v2._networkless_proof)
    proxy_environment = inspect.getsource(artifacts_v2._proxied_environment)

    assert "execution" not in signature.parameters
    assert "pnpm_distribution" not in signature.parameters
    assert "npm_distribution" not in signature.parameters
    assert source.count("_download(") == 2
    assert "sanitized_environment" in source
    assert "_extract_distribution_tree" in source
    assert "--audit=false" in source
    assert "--fund=false" in source
    assert "--ignore-pnpmfile" in source
    assert "--registry=https://registry.npmjs.org/" in source
    assert "_registry_proxy" in source
    assert "NPM_CONFIG_HTTPS_PROXY" in proxy_environment
    assert "--network=none" in proof
    assert "--offline" in proof


def test_final_materialization_does_not_copy_multi_gigabyte_attempt_bytes() -> None:
    source = inspect.getsource(prepare_offline_dependency_artifacts)

    assert source.count("_link_regular(") == 2
    assert "_copy_regular(openclaw.path" not in source
    assert "_copy_regular(bridge.path" not in source


def test_real_acquisition_is_explicitly_gated_and_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(
        "PERSONAL_OPERATOR_RUN_OFFLINE_DEPENDENCY_ACQUISITION", raising=False
    )
    output = tmp_path / "output"

    with pytest.raises(ArtifactGenerationError, match="explicitly enabled"):
        prepare_offline_dependency_artifacts(
            openclaw_repository=tmp_path / "openclaw",
            release_repository=tmp_path / "release",
            output=output,
        )

    assert not output.exists()


def test_cli_has_no_aws_or_dynamic_provider_surface() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts/prepare-offline-dependency-artifacts-v2.py"
    ).read_text(encoding="utf-8")
    module = Path(__file__).with_name(
        "offline_dependency_artifacts_v2.py"
    ).read_text(encoding="utf-8")

    combined = source + module
    assert "boto" + "3" not in combined
    assert "aws" + "cli" not in combined.lower()
    assert "importlib" not in combined
    assert "shell=True" not in combined
    assert "PERSONAL_OPERATOR_RUN_OFFLINE_DEPENDENCY_ACQUISITION" in combined


def test_cli_argument_errors_are_generic_and_do_not_echo_values() -> None:
    script = (
        Path(__file__).parents[1]
        / "scripts/prepare-offline-dependency-artifacts-v2.py"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--openclaw-repository",
            "/tmp/openclaw",
            "--release-repository",
            "/tmp/release",
            "--output",
            "/tmp/output",
            "--unknown",
            "SECRET-ARG",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"offline dependency artifact generation rejected\n"
    assert b"SECRET-ARG" not in completed.stderr


def test_final_set_manifest_binds_exact_npm_distribution_digest() -> None:
    payload = artifacts_v2._artifact_set_manifest(
        openclaw=AttemptArtifact("a" * 64, 10),
        bridge=AttemptArtifact("b" * 64, 20),
    )

    bridge = json.loads(payload)["bridge"]
    assert bridge["distributionSha512"] == BRIDGE_NPM_DISTRIBUTION_SHA512
