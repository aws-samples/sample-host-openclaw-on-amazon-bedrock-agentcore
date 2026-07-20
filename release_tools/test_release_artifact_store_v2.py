from __future__ import annotations

from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

from release_tools.contracts import (
    MAX_CONTRACT_BYTES,
    PRIVATE_MUTATION_ENVELOPE_MAGIC,
    PrivateMutationEnvelopeV2,
    canonical_json_bytes,
)
from release_tools.release_artifact_store_v2 import (
    ReleaseArtifactStoreV2,
    ReleaseArtifactStoreV2Error,
    _MAX_PAYLOAD_BYTES,
    _require_directory,
    _require_record,
)
from release_tools.release_plan_v2 import (
    AssembledReleasePlanV2,
    ReleasePlanAssemblerV2,
)
from release_tools.test_release_plan_v2 import _preclosed_source
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
)


@pytest.fixture(scope="module")
def assembled(tmp_path_factory: pytest.TempPathFactory) -> AssembledReleasePlanV2:
    return ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path_factory.mktemp("artifact-store-source"))
    )


def _persisted(
    root: Path, assembled: AssembledReleasePlanV2
) -> tuple[ReleaseArtifactStoreV2, str, Path]:
    store = ReleaseArtifactStoreV2.create(root)
    bundle = store.persist(assembled)
    digest = bundle.plan_sha256
    bundle.close()
    return store, digest, root / digest


def _rewrite_record(path: Path, payload: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(payload)
    path.chmod(0o400)


def _replace_record(path: Path, payload: bytes) -> None:
    alternate = path.with_name(path.name + ".replacement")
    alternate.write_bytes(payload)
    alternate.chmod(0o400)
    os.replace(alternate, path)


def _inventory(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "inventory.json").read_bytes())


def _uncertain_current_mutation(
    root: Path,
    assembled: AssembledReleasePlanV2,
):
    journal = _create_v2(root, assembled.plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "foundation:BOOTSTRAP_STACK")
    journal.begin_step()
    step = assembled.plan.steps[journal.current.completed_step_count]
    payload = dict(assembled.payloads)[step.request_artifact]
    resolved = _resolved_mutation_request(
        journal,
        request_artifact_size=len(payload),
    )
    return journal, resolved, payload


def _assembled_with_mutation_payload(
    assembled: AssembledReleasePlanV2,
    payload: bytes,
) -> AssembledReleasePlanV2:
    step = assembled.plan.steps[1]
    assert step.kind == "BOOTSTRAP_STACK"
    digest = hashlib.sha256(payload).hexdigest()
    artifacts = tuple(
        replace(item, size=len(payload), sha256=digest)
        if item.path == step.request_artifact
        else item
        for item in assembled.plan.artifacts
    )
    steps = tuple(
        replace(
            item,
            request_sha256=digest,
            expected_request_sha256=digest,
        )
        if item.ordinal == step.ordinal
        else item
        for item in assembled.plan.steps
    )
    plan = replace(assembled.plan, artifacts=artifacts, steps=steps)
    payloads = tuple(
        (path, payload if path == step.request_artifact else body)
        for path, body in assembled.payloads
    )
    return AssembledReleasePlanV2(
        plan=plan,
        payloads=payloads,
        stages=assembled.stages,
    )


def test_store_persists_and_reopens_exact_plan_and_payload_inventory(
    tmp_path: Path, assembled: AssembledReleasePlanV2
) -> None:
    expected_plan = assembled.plan.to_bytes()
    expected_digest = hashlib.sha256(expected_plan).hexdigest()
    with ReleaseArtifactStoreV2.create(tmp_path / "store") as store:
        created = store.persist(assembled)
        created.close()

    with ReleaseArtifactStoreV2.open(tmp_path / "store") as reopened_store:
        with reopened_store.reopen(expected_digest) as reopened:
            assert reopened.plan == assembled.plan
            assert reopened.plan_sha256 == expected_digest
            assert reopened.plan_size == len(expected_plan)
            assert reopened.artifacts == tuple(assembled.plan.artifacts)
            for path, payload in assembled.payloads:
                assert (
                    b"".join(reopened.iter_verified_chunks(path, chunk_size=7))
                    == payload
                )


def test_reopened_bundle_exposes_no_root_path_descriptor_or_mutable_bytes(
    tmp_path: Path, assembled: AssembledReleasePlanV2
) -> None:
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    store.close()
    with ReleaseArtifactStoreV2.open(tmp_path / "store") as reopened_store:
        with reopened_store.reopen(digest) as bundle:
            public = {name for name in dir(bundle) if not name.startswith("_")}
            assert public == {
                "artifacts",
                "close",
                "iter_verified_chunks",
                "plan",
                "plan_sha256",
                "plan_size",
                "write_private_mutation_envelope",
            }
            first_path, first_payload = assembled.payloads[0]
            chunk = next(bundle.iter_verified_chunks(first_path, chunk_size=3))
            assert isinstance(chunk, bytes)
            assert chunk == first_payload[:3]


@pytest.mark.parametrize("attack", ("missing", "extra", "duplicate", "unsafe"))
def test_persist_rejects_nonexact_or_unsafe_in_memory_payload_inventory(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    payloads = assembled.payloads
    if attack == "missing":
        hostile = payloads[:-1]
    elif attack == "extra":
        hostile = (*payloads, ("build/release-requests/extra.json", b"{}\n"))
    elif attack == "duplicate":
        hostile = (*payloads, payloads[-1])
    else:
        hostile = (*payloads, ("../outside.private", b"hostile"))
    with ReleaseArtifactStoreV2.create(tmp_path / "store") as store:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="inventory|path"):
            store.persist(replace(assembled, payloads=hostile))


def test_persist_rejects_payload_substitution_and_crossed_plan_bundle(
    tmp_path: Path, assembled: AssembledReleasePlanV2
) -> None:
    path, payload = assembled.payloads[0]
    crossed_payloads = ((path, payload + b"x"), *assembled.payloads[1:])
    with ReleaseArtifactStoreV2.create(tmp_path / "substitution") as store:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="plan binding"):
            store.persist(replace(assembled, payloads=crossed_payloads))

    other = ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path / "other-source", extra_assets=1)
    )
    with ReleaseArtifactStoreV2.create(tmp_path / "crossed") as store:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="inventory"):
            store.persist(replace(assembled, payloads=other.payloads))


def test_create_is_exclusive_and_open_rejects_root_symlink_or_mode_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    with ReleaseArtifactStoreV2.create(root):
        pass
    with pytest.raises(ReleaseArtifactStoreV2Error, match="created"):
        ReleaseArtifactStoreV2.create(root)
    root.chmod(0o750)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="mode"):
        ReleaseArtifactStoreV2.open(root)
    root.chmod(0o700)
    link = tmp_path / "store-link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="opened"):
        ReleaseArtifactStoreV2.open(link)


def test_owner_and_mode_checks_fail_closed() -> None:
    current = os.stat_result(
        (0o100400, 0, 0, 1, os.geteuid(), 0, 1, 1, 1, 1)
    )
    hostile_owner = SimpleNamespace(**{
        name: getattr(current, name)
        for name in (
            "st_mode", "st_dev", "st_ino", "st_uid", "st_nlink", "st_size",
            "st_mtime_ns", "st_ctime_ns",
        )
    })
    hostile_owner.st_uid = os.geteuid() + 1
    with pytest.raises(ReleaseArtifactStoreV2Error, match="owner"):
        _require_record(hostile_owner, label="record")  # type: ignore[arg-type]

    directory = SimpleNamespace(**hostile_owner.__dict__)
    directory.st_mode = 0o40750
    directory.st_uid = os.geteuid()
    with pytest.raises(ReleaseArtifactStoreV2Error, match="mode"):
        _require_directory(directory, label="directory")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "attack", ("plan-mode", "bundle-mode", "truncate", "substitute", "symlink", "hardlink")
)
def test_reopen_rejects_mode_content_link_and_type_tampering(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    store, digest, bundle = _persisted(tmp_path / "store", assembled)
    inventory = _inventory(bundle)
    payload = bundle / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    if attack == "plan-mode":
        (bundle / "plan.json").chmod(0o600)
    elif attack == "bundle-mode":
        bundle.chmod(0o750)
    elif attack == "truncate":
        _rewrite_record(payload, payload.read_bytes()[:-1])
    elif attack == "substitute":
        body = bytearray(payload.read_bytes())
        body[0] ^= 1
        _rewrite_record(payload, bytes(body))
    elif attack == "symlink":
        body = payload.read_bytes()
        payload.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(body)
        outside.chmod(0o400)
        payload.symlink_to(outside)
    else:
        os.link(payload, tmp_path / "alias")
    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.reopen(digest)
    store.close()


@pytest.mark.parametrize("attack", ("missing", "extra"))
def test_reopen_rejects_missing_or_extra_payload_records(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    store, digest, bundle = _persisted(tmp_path / "store", assembled)
    inventory = _inventory(bundle)
    payload = bundle / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    if attack == "missing":
        payload.unlink()
    else:
        extra = bundle / "payload-99999999.bin"
        extra.write_bytes(b"extra")
        extra.chmod(0o400)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="missing|extra|unavailable"):
        store.reopen(digest)
    store.close()


def test_reopen_rejects_plan_and_inventory_substitution(
    tmp_path: Path, assembled: AssembledReleasePlanV2
) -> None:
    store, digest, bundle = _persisted(tmp_path / "plan", assembled)
    body = bytearray((bundle / "plan.json").read_bytes())
    body[-2] ^= 1
    _rewrite_record(bundle / "plan.json", bytes(body))
    with pytest.raises(ReleaseArtifactStoreV2Error, match="plan|digest"):
        store.reopen(digest)
    store.close()

    store, digest, bundle = _persisted(tmp_path / "inventory", assembled)
    inventory = _inventory(bundle)
    inventory["artifacts"][0]["path"] = "../outside.private"  # type: ignore[index]
    _rewrite_record(
        bundle / "inventory.json", canonical_json_bytes(inventory)
    )
    with pytest.raises(ReleaseArtifactStoreV2Error, match="inventory|path"):
        store.reopen(digest)
    store.close()


def test_reopen_rejects_crossed_plan_directory(
    tmp_path: Path, assembled: AssembledReleasePlanV2
) -> None:
    store, digest, bundle = _persisted(tmp_path / "store", assembled)
    crossed_digest = "f" * 64 if digest != "f" * 64 else "e" * 64
    crossed = bundle.with_name(crossed_digest)
    bundle.rename(crossed)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="plan|bundle"):
        store.reopen(crossed_digest)
    store.close()


def test_reopen_rejects_bundle_replacement_race(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, bundle = _persisted(tmp_path / "store", assembled)
    fired = False

    def race(stage: str, _path: str) -> None:
        nonlocal fired
        if stage == "bundle-before-open" and not fired:
            fired = True
            bundle.rename(bundle.with_name("retained-old"))
            bundle.mkdir(mode=0o700)

    monkeypatch.setattr(subject, "_stability_hook", race)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="replaced"):
        store.reopen(digest)
    store.close()


def test_reopen_rejects_record_replacement_and_truncation_races(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, bundle = _persisted(tmp_path / "replace", assembled)
    inventory = _inventory(bundle)
    logical = inventory["artifacts"][0]["path"]  # type: ignore[index]
    record = bundle / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    fired = False

    def replace_race(stage: str, path: str) -> None:
        nonlocal fired
        if stage == "record-before-open" and path == logical and not fired:
            fired = True
            _replace_record(record, record.read_bytes())

    monkeypatch.setattr(subject, "_stability_hook", replace_race)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="replaced"):
        store.reopen(digest)
    store.close()

    store, digest, bundle = _persisted(tmp_path / "truncate", assembled)
    inventory = _inventory(bundle)
    logical = inventory["artifacts"][0]["path"]  # type: ignore[index]
    record = bundle / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    fired = False

    def truncate_race(stage: str, path: str) -> None:
        nonlocal fired
        if stage == "record-after-read" and path == logical and not fired:
            fired = True
            _rewrite_record(record, record.read_bytes()[:-1])

    monkeypatch.setattr(subject, "_stability_hook", truncate_race)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="size|changed"):
        store.reopen(digest)
    store.close()


def test_verified_chunk_reader_rejects_replacement_and_in_place_race_before_yield(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, bundle_path = _persisted(tmp_path / "replace", assembled)
    inventory = _inventory(bundle_path)
    logical = inventory["artifacts"][0]["path"]  # type: ignore[index]
    record = bundle_path / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    bundle = store.reopen(digest)
    _replace_record(record, record.read_bytes())
    with pytest.raises(ReleaseArtifactStoreV2Error, match="identity"):
        next(bundle.iter_verified_chunks(logical))
    bundle.close()
    store.close()

    store, digest, bundle_path = _persisted(tmp_path / "mutate", assembled)
    inventory = _inventory(bundle_path)
    logical = inventory["artifacts"][0]["path"]  # type: ignore[index]
    record = bundle_path / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    bundle = store.reopen(digest)
    fired = False

    def mutate_race(stage: str, path: str) -> None:
        nonlocal fired
        if stage == "chunk-before-yield" and path == logical and not fired:
            fired = True
            body = bytearray(record.read_bytes())
            body[0] ^= 1
            _rewrite_record(record, bytes(body))

    monkeypatch.setattr(subject, "_stability_hook", mutate_race)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="changed"):
        next(bundle.iter_verified_chunks(logical))
    bundle.close()
    store.close()


def test_streaming_payload_boundary_is_distinct_from_canonical_plan_boundary() -> None:
    digest = "a" * 64
    base = {
        "schema": "personal-operator.release-artifact-bundle.v2",
        "planSha256": digest,
        "planSize": MAX_CONTRACT_BYTES,
        "artifacts": [
            {
                "path": "build/release-requests/large.private",
                "size": _MAX_PAYLOAD_BYTES,
                "sha256": "b" * 64,
                "storage": "payload-00000000.bin",
            }
        ],
    }
    plan_size, artifacts = ReleaseArtifactStoreV2._parse_inventory(
        canonical_json_bytes(base), expected_plan_sha256=digest
    )
    assert plan_size == MAX_CONTRACT_BYTES
    assert artifacts[0][1] == 8 * 1024 * 1024 * 1024
    base["planSize"] = MAX_CONTRACT_BYTES + 1
    with pytest.raises(ReleaseArtifactStoreV2Error, match="plan size"):
        ReleaseArtifactStoreV2._parse_inventory(
            canonical_json_bytes(base), expected_plan_sha256=digest
        )
    base["planSize"] = MAX_CONTRACT_BYTES
    base["artifacts"][0]["size"] = _MAX_PAYLOAD_BYTES + 1  # type: ignore[index]
    with pytest.raises(ReleaseArtifactStoreV2Error, match="payload size"):
        ReleaseArtifactStoreV2._parse_inventory(
            canonical_json_bytes(base), expected_plan_sha256=digest
        )


def test_verified_reader_bounds_each_filesystem_read(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, _ = _persisted(tmp_path / "store", assembled)
    bundle = store.reopen(digest)
    path, payload = assembled.payloads[0]
    requested: list[int] = []
    original = os.read

    def bounded(descriptor: int, size: int) -> bytes:
        requested.append(size)
        return original(descriptor, size)

    monkeypatch.setattr(subject.os, "read", bounded)
    assert b"".join(
        bundle.iter_verified_chunks(path, chunk_size=16 * 1024 * 1024)
    ) == payload
    assert requested and max(requested) <= 1024 * 1024
    bundle.close()
    store.close()


def test_verified_reader_is_stable_across_short_os_reads(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, _ = _persisted(tmp_path / "store", assembled)
    bundle = store.reopen(digest)
    path, payload = assembled.payloads[0]
    original = os.read

    def short(descriptor: int, size: int) -> bytes:
        return original(descriptor, min(size, 3))

    monkeypatch.setattr(subject.os, "read", short)
    assert b"".join(bundle.iter_verified_chunks(path, chunk_size=11)) == payload
    bundle.close()
    store.close()


def test_persist_excludes_reopen_until_final_directory_fsync(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    writer = ReleaseArtifactStoreV2.create(tmp_path / "store")
    reader = ReleaseArtifactStoreV2.open(tmp_path / "store")
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    before_fsync = threading.Event()
    allow_fsync = threading.Event()
    reader_started = threading.Event()
    reader_done = threading.Event()
    errors: list[BaseException] = []
    bundles: list[object] = []

    def pause(stage: str, _path: str) -> None:
        if stage == "persist-before-final-fsync":
            before_fsync.set()
            if not allow_fsync.wait(5):
                raise AssertionError("test did not release final fsync")

    def persist() -> None:
        try:
            bundles.append(writer.persist(assembled))
        except BaseException as error:
            errors.append(error)

    def reopen() -> None:
        reader_started.set()
        try:
            bundles.append(reader.reopen(digest))
        except BaseException as error:
            errors.append(error)
        finally:
            reader_done.set()

    monkeypatch.setattr(subject, "_stability_hook", pause)
    writer_thread = threading.Thread(target=persist)
    writer_thread.start()
    assert before_fsync.wait(5), "persist never reached its durability boundary"
    reader_thread = threading.Thread(target=reopen)
    reader_thread.start()
    assert reader_started.wait(1)
    assert not reader_done.wait(0.2), "reader observed a precommit bundle"
    allow_fsync.set()
    writer_thread.join(5)
    reader_thread.join(5)
    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []
    assert len(bundles) == 2
    for bundle in bundles:
        bundle.close()  # type: ignore[attr-defined]
    writer.close()
    reader.close()


def test_exact_plan_retry_recovers_interrupted_staging_after_plan_record(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store = ReleaseArtifactStoreV2.create(tmp_path / "store")
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    interrupted = False

    def interrupt(stage: str, _path: str) -> None:
        nonlocal interrupted
        if stage == "persist-after-plan" and not interrupted:
            interrupted = True
            raise ReleaseArtifactStoreV2Error("synthetic writer interruption")

    monkeypatch.setattr(subject, "_stability_hook", interrupt)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="interruption"):
        store.persist(assembled)
    assert not (tmp_path / "store" / digest).exists()

    monkeypatch.setattr(subject, "_stability_hook", lambda *_args: None)
    with store.persist(assembled) as bundle:
        assert bundle.plan_sha256 == digest
    store.close()


@pytest.mark.parametrize("attack", ("bytes", "inode"))
def test_intent_substitution_after_plan_is_preserved_and_never_unlinked(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    intent = root / f"INPROGRESS-{digest}.json"
    hostile = canonical_json_bytes(
        {
            "schema": "personal-operator.hostile-intent.v1",
            "planSha256": digest,
        }
    )
    substituted = b""
    hostile_inode = 0
    fired = False

    def substitute(stage: str, _path: str) -> None:
        nonlocal fired, hostile_inode, substituted
        if stage == "persist-after-plan" and not fired:
            fired = True
            substituted = intent.read_bytes() if attack == "inode" else hostile
            _replace_record(intent, substituted)
            hostile_inode = intent.stat(follow_symlinks=False).st_ino

    monkeypatch.setattr(subject, "_stability_hook", substitute)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="intent"):
        store.persist(assembled)

    assert fired
    assert intent.stat(follow_symlinks=False).st_ino == hostile_inode
    assert intent.stat(follow_symlinks=False).st_mode & 0o777 == 0o400
    assert intent.read_bytes() == substituted
    store.close()


@pytest.mark.parametrize("attack", ("bytes", "inode"))
def test_final_intent_boundary_never_unlinks_a_crossed_path(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    intent = root / f"INPROGRESS-{digest}.json"
    original_unlink = os.unlink
    fired = False

    def cross_then_unlink(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal fired
        if path == intent.name and dir_fd == store._root_fd:
            fired = True
            original = intent.read_bytes()
            replacement = (
                canonical_json_bytes(
                    {
                        "schema": "personal-operator.crossed-intent.v1",
                        "planSha256": digest,
                    }
                )
                if attack == "bytes"
                else original
            )
            _replace_record(intent, replacement)
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(subject.os, "unlink", cross_then_unlink)
    with store.persist(assembled) as bundle:
        assert bundle.plan_sha256 == digest

    assert not fired
    assert intent.is_file()
    assert intent.stat(follow_symlinks=False).st_mode & 0o777 == 0o400
    assert (root / f"CONSUMED-{digest}.json").is_file()
    store.close()


@pytest.mark.parametrize("attack", ("bytes", "inode"))
def test_consumed_pair_rejects_and_preserves_retained_intent_substitution(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    root = tmp_path / "store"
    store, digest, _bundle = _persisted(root, assembled)
    intent = root / f"INPROGRESS-{digest}.json"
    replacement = (
        canonical_json_bytes(
            {
                "schema": "personal-operator.crossed-intent.v1",
                "planSha256": digest,
            }
        )
        if attack == "bytes"
        else intent.read_bytes()
    )
    _replace_record(intent, replacement)
    hostile_inode = intent.stat(follow_symlinks=False).st_ino

    with pytest.raises(ReleaseArtifactStoreV2Error, match="intent|consumed"):
        store.reopen(digest)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="intent|consumed"):
        store.persist(assembled)
    assert intent.stat(follow_symlinks=False).st_ino == hostile_inode
    assert intent.read_bytes() == replacement
    store.close()


def test_committed_bundle_with_unconsumed_intent_finishes_append_only_recovery(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    interrupted = False

    def interrupt(stage: str, _path: str) -> None:
        nonlocal interrupted
        if stage == "persist-before-intent-consumption-record" and not interrupted:
            interrupted = True
            raise ReleaseArtifactStoreV2Error("synthetic consumption interruption")

    monkeypatch.setattr(subject, "_stability_hook", interrupt)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="interruption"):
        store.persist(assembled)
    assert interrupted
    assert (root / digest / "COMMITTED").is_file()
    assert (root / f"INPROGRESS-{digest}.json").is_file()
    assert not (root / f"CONSUMED-{digest}.json").exists()
    with pytest.raises(ReleaseArtifactStoreV2Error, match="in.?progress|intent"):
        store.reopen(digest)

    monkeypatch.setattr(subject, "_stability_hook", lambda *_args: None)
    with store.persist(assembled) as recovered:
        assert recovered.plan_sha256 == digest
    assert (root / f"INPROGRESS-{digest}.json").is_file()
    assert (root / f"CONSUMED-{digest}.json").is_file()
    store.close()


def test_close_revokes_partially_consumed_verified_reader_before_next_yield(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    bundle = store.reopen(digest)
    path, payload = assembled.payloads[0]
    assert len(payload) > 1
    chunks = bundle.iter_verified_chunks(path, chunk_size=1)
    assert next(chunks) == payload[:1]

    bundle.close()

    with pytest.raises(ReleaseArtifactStoreV2Error, match="closed|revoked"):
        next(chunks)
    store.close()


def test_post_rename_root_fsync_eio_is_uncommitted_and_retry_repairs(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store = ReleaseArtifactStoreV2.create(tmp_path / "store")
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    original_fsync = os.fsync
    after_rename = False
    failed = False

    def mark(stage: str, _path: str) -> None:
        nonlocal after_rename
        if stage == "persist-before-final-fsync":
            after_rename = True

    def fail_post_rename_root_fsync(descriptor: int) -> None:
        nonlocal failed
        if after_rename and descriptor == store._root_fd and not failed:
            failed = True
            raise OSError(errno.EIO, "synthetic post-rename fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(subject, "_stability_hook", mark)
    monkeypatch.setattr(subject.os, "fsync", fail_post_rename_root_fsync)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="persisted"):
        store.persist(assembled)
    assert failed
    assert (tmp_path / "store" / digest).is_dir()

    monkeypatch.setattr(subject, "_stability_hook", lambda *_args: None)
    monkeypatch.setattr(subject.os, "fsync", original_fsync)
    with pytest.raises(
        ReleaseArtifactStoreV2Error, match="commit|incomplete|in.?progress|intent"
    ):
        store.reopen(digest)

    with store.persist(assembled) as repaired:
        assert repaired.plan_sha256 == digest
        assert b"".join(
            repaired.iter_verified_chunks(assembled.payloads[0][0])
        ) == assembled.payloads[0][1]
    store.close()


def test_concurrent_close_barrier_revokes_pending_visible_yield(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, _ = _persisted(tmp_path / "store", assembled)
    bundle = store.reopen(digest)
    path, _payload = assembled.payloads[0]
    chunks = bundle.iter_verified_chunks(path, chunk_size=1)
    at_handoff = threading.Event()
    allow_handoff = threading.Event()
    close_started = threading.Event()
    close_returned = threading.Event()
    yielded: list[bytes] = []
    errors: list[BaseException] = []

    def pause(stage: str, logical_path: str) -> None:
        if (
            stage == "chunk-before-visible-yield"
            and logical_path == path
            and not at_handoff.is_set()
        ):
            at_handoff.set()
            if not allow_handoff.wait(5):
                raise AssertionError("test did not release visible handoff")

    def consume() -> None:
        try:
            yielded.append(next(chunks))
        except BaseException as error:
            errors.append(error)

    def close() -> None:
        close_started.set()
        bundle.close()
        close_returned.set()

    monkeypatch.setattr(subject, "_stability_hook", pause)
    consumer = threading.Thread(target=consume)
    consumer.start()
    assert at_handoff.wait(5), "reader never reached visible handoff"
    closer = threading.Thread(target=close)
    closer.start()
    assert close_started.wait(1)
    assert bundle._close_requested.wait(1)
    assert not close_returned.is_set()
    allow_handoff.set()
    consumer.join(5)
    closer.join(5)
    assert not consumer.is_alive()
    assert not closer.is_alive()
    assert yielded == []
    assert len(errors) == 1
    assert isinstance(errors[0], ReleaseArtifactStoreV2Error)
    assert close_returned.is_set()
    store.close()


def test_hard_exit_before_commit_directory_fsync_leaves_recoverable_intent(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    ReleaseArtifactStoreV2.create(root).close()
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    process = os.fork()
    if process == 0:
        try:
            def hard_exit(stage: str, _path: str) -> None:
                if stage == "persist-before-commit-directory-fsync":
                    os._exit(73)

            subject._stability_hook = hard_exit
            ReleaseArtifactStoreV2.open(root).persist(assembled)
        finally:
            os._exit(91)
    waited, status = os.waitpid(process, 0)
    assert waited == process
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 73

    with ReleaseArtifactStoreV2.open(root) as store:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="in.?progress|intent"):
            store.reopen(digest)
        with store.persist(assembled) as repaired:
            assert repaired.plan_sha256 == digest


def test_commit_directory_fsync_eio_retains_intent_for_new_process_recovery(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    original_fsync = os.fsync
    fail_next = False
    failed = False

    def mark(stage: str, _path: str) -> None:
        nonlocal fail_next
        if stage == "persist-before-commit-directory-fsync":
            fail_next = True

    def fail_commit_directory_fsync(descriptor: int) -> None:
        nonlocal fail_next, failed
        if fail_next and not failed:
            fail_next = False
            failed = True
            raise OSError(errno.EIO, "synthetic commit directory fsync EIO")
        original_fsync(descriptor)

    monkeypatch.setattr(subject, "_stability_hook", mark)
    monkeypatch.setattr(subject.os, "fsync", fail_commit_directory_fsync)
    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.persist(assembled)
    store.close()
    assert failed

    monkeypatch.setattr(subject, "_stability_hook", lambda *_args: None)
    monkeypatch.setattr(subject.os, "fsync", original_fsync)
    with ReleaseArtifactStoreV2.open(root) as restarted:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="in.?progress|intent"):
            restarted.reopen(digest)
        with restarted.persist(assembled) as repaired:
            assert repaired.plan_sha256 == digest


def test_intent_consumption_fsync_eio_retains_exact_pair_for_reconciliation(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    original_fsync = os.fsync
    fail_next = False
    failed = False

    def mark(stage: str, _path: str) -> None:
        nonlocal fail_next
        if stage == "persist-before-intent-removal-root-fsync":
            fail_next = True

    def fail_intent_removal_fsync(descriptor: int) -> None:
        nonlocal fail_next, failed
        if fail_next and not failed:
            fail_next = False
            failed = True
            raise OSError(errno.EIO, "synthetic intent removal fsync EIO")
        original_fsync(descriptor)

    monkeypatch.setattr(subject, "_stability_hook", mark)
    monkeypatch.setattr(subject.os, "fsync", fail_intent_removal_fsync)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="intent consumption"):
        store.persist(assembled)
    assert failed
    assert (root / f"INPROGRESS-{digest}.json").is_file()
    assert (root / f"CONSUMED-{digest}.json").is_file()
    store.close()

    monkeypatch.setattr(subject, "_stability_hook", lambda *_args: None)
    monkeypatch.setattr(subject.os, "fsync", original_fsync)
    with ReleaseArtifactStoreV2.open(root) as restarted:
        with restarted.reopen(digest) as reopened:
            assert reopened.plan_sha256 == digest
        with restarted.persist(assembled) as repaired:
            assert repaired.plan_sha256 == digest


def test_persist_preserves_invalid_committed_bundle_without_recovery_intent(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    store, _digest_value, bundle = _persisted(tmp_path / "store", assembled)
    inventory = _inventory(bundle)
    record = bundle / inventory["artifacts"][0]["storage"]  # type: ignore[index]
    body = bytearray(record.read_bytes())
    body[0] ^= 1
    _rewrite_record(record, bytes(body))
    hostile_inode = record.stat().st_ino
    hostile_bytes = record.read_bytes()

    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.persist(assembled)

    assert record.stat().st_ino == hostile_inode
    assert record.read_bytes() == hostile_bytes
    store.close()


def test_close_returns_for_abandoned_partially_consumed_iterator(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    bundle = store.reopen(digest)
    path, payload = assembled.payloads[0]
    chunks = bundle.iter_verified_chunks(path, chunk_size=1)
    assert next(chunks) == payload[:1]
    close_returned = threading.Event()

    def close() -> None:
        bundle.close()
        close_returned.set()

    closer = threading.Thread(target=close, daemon=True)
    closer.start()
    assert close_returned.wait(1), "close deadlocked behind an abandoned iterator"
    closer.join(1)
    assert not closer.is_alive()
    with pytest.raises(ReleaseArtifactStoreV2Error, match="closed|revoked"):
        next(chunks)
    store.close()


def test_hard_exit_after_intent_unlink_reopen_fsyncs_exact_committed_bundle(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    ReleaseArtifactStoreV2.create(root).close()
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    process = os.fork()
    if process == 0:
        try:
            def hard_exit(stage: str, _path: str) -> None:
                if stage == "persist-before-intent-removal-root-fsync":
                    os._exit(74)

            subject._stability_hook = hard_exit
            ReleaseArtifactStoreV2.open(root).persist(assembled)
        finally:
            os._exit(91)
    waited, status = os.waitpid(process, 0)
    assert waited == process
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 74

    original_fsync = os.fsync
    reconciled_modes: list[int] = []

    def observe_fsync(descriptor: int) -> None:
        reconciled_modes.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(subject.os, "fsync", observe_fsync)
    with ReleaseArtifactStoreV2.open(root) as restarted:
        with restarted.reopen(digest) as bundle:
            assert bundle.plan_sha256 == digest
    regular = [mode for mode in reconciled_modes if (mode & 0o170000) == 0o100000]
    directories = [mode for mode in reconciled_modes if (mode & 0o170000) == 0o040000]
    assert len(regular) >= len(assembled.payloads) + 3
    assert len(directories) >= 2


def test_reopen_rejects_any_exact_bundle_reconciliation_fsync_failure(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    store, digest, _ = _persisted(tmp_path / "store", assembled)
    failed = False

    def fail_reconciliation(_descriptor: int) -> None:
        nonlocal failed
        failed = True
        raise OSError(errno.EIO, "synthetic reconciliation fsync EIO")

    monkeypatch.setattr(subject.os, "fsync", fail_reconciliation)
    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.reopen(digest)
    assert failed
    store.close()


def test_forked_partial_intent_write_is_bounded_and_retryable(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    root = tmp_path / "store"
    ReleaseArtifactStoreV2.create(root).close()
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    process = os.fork()
    if process == 0:
        try:
            def hard_exit(stage: str, _path: str) -> None:
                if stage == "persist-intent-partial-write":
                    os._exit(75)

            subject._stability_hook = hard_exit
            ReleaseArtifactStoreV2.open(root).persist(assembled)
        finally:
            os._exit(91)
    waited, status = os.waitpid(process, 0)
    assert waited == process
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 75
    assert not (root / f"INPROGRESS-{digest}.json").exists()
    temporary = root / f".INPROGRESS-{digest}.json.tmp"
    assert temporary.is_file()
    assert temporary.stat().st_mode & 0o777 == 0o600

    with ReleaseArtifactStoreV2.open(root) as restarted:
        with restarted.persist(assembled) as repaired:
            assert repaired.plan_sha256 == digest
    assert not temporary.exists()


@pytest.mark.parametrize("attack", ("fanout", "symlink", "hardlink", "directory"))
def test_intent_temp_recovery_rejects_fanout_and_substitution(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    temporary = root / f".INPROGRESS-{digest}.json.tmp"
    if attack == "fanout":
        for ordinal in range(12):
            candidate = root / (
                f".INPROGRESS-{digest}.json.tmp.hostile-{ordinal:02d}"
            )
            candidate.write_bytes(b"hostile")
            candidate.chmod(0o400)
    elif attack == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"hostile")
        temporary.symlink_to(outside)
    elif attack == "hardlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"hostile")
        outside.chmod(0o400)
        os.link(outside, temporary)
    else:
        temporary.mkdir(mode=0o700)

    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.persist(assembled)

    if attack == "fanout":
        assert len(tuple(root.glob(f".INPROGRESS-{digest}*"))) == 12
    else:
        assert temporary.exists() or temporary.is_symlink()
    store.close()


def test_consumed_intent_substitution_is_preserved_and_rejected(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    root = tmp_path / "store"
    store, digest, _bundle = _persisted(root, assembled)
    consumed = root / f"CONSUMED-{digest}.json"
    replacement = canonical_json_bytes(
        {
            "schema": "personal-operator.crossed-consumption.v1",
            "planSha256": digest,
        }
    )
    _replace_record(consumed, replacement)
    hostile_inode = consumed.stat(follow_symlinks=False).st_ino

    with pytest.raises(ReleaseArtifactStoreV2Error, match="consumed intent"):
        store.reopen(digest)
    with pytest.raises(ReleaseArtifactStoreV2Error, match="consumed intent"):
        store.persist(assembled)
    assert consumed.stat(follow_symlinks=False).st_ino == hostile_inode
    assert consumed.read_bytes() == replacement
    store.close()


def test_malformed_final_intent_is_never_cleaned_or_promoted(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    root = tmp_path / "store"
    store = ReleaseArtifactStoreV2.create(root)
    digest = hashlib.sha256(assembled.plan.to_bytes()).hexdigest()
    intent = root / f"INPROGRESS-{digest}.json"
    intent.write_bytes(b"partial-hostile-intent")
    assert intent.stat().st_mode & 0o777 == 0o644
    intent.chmod(0o600)
    inode = intent.stat().st_ino
    payload = intent.read_bytes()

    with pytest.raises(ReleaseArtifactStoreV2Error):
        store.persist(assembled)

    assert intent.stat().st_ino == inode
    assert intent.stat().st_mode & 0o777 == 0o600
    assert intent.read_bytes() == payload
    store.close()


def test_bundle_streams_only_exact_current_artifact_into_private_envelope(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    journal, resolved, payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"

    with store.reopen(digest) as bundle:
        metadata = bundle.write_private_mutation_envelope(
            target,
            resolved_request=resolved,
            transaction=journal.current,
        )

    details = target.stat(follow_symlinks=False)
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert details.st_uid == os.geteuid()
    assert details.st_nlink == 1
    assert target.read_bytes().startswith(PRIVATE_MUTATION_ENVELOPE_MAGIC)
    assert metadata.resolved_request == resolved
    assert metadata.request_artifact_size == len(payload)
    assert metadata.request_artifact_sha256 == hashlib.sha256(payload).hexdigest()
    assert tuple(target_dir.glob(f".{target.name}.*.tmp")) == ()
    with PrivateMutationEnvelopeV2.open_verified(
        target,
        plan=assembled.plan,
        transaction=journal.current,
        scratch_dir=tmp_path / "verified-scratch",
    ) as verified:
        assert b"".join(verified.iter_artifact_chunks(chunk_size=17)) == payload
    store.close()


@pytest.mark.parametrize("attack", ("stale-prefix", "cross-plan"))
def test_envelope_bridge_rejects_stale_or_crossed_transaction_authority(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    if attack == "stale-prefix":
        resolved = replace(
            resolved,
            mutation_request=replace(
                resolved.mutation_request,
                completed_prefix_sha256="f" * 64,
            ),
        )
        transaction = journal.current
    else:
        transaction = replace(journal.current, plan_sha256="f" * 64)
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"

    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="binding|canonical"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=transaction,
            )
    assert not target.exists()
    assert tuple(target_dir.iterdir()) == ()
    store.close()


@pytest.mark.parametrize(
    "attack",
    ("record-replacement", "bundle-close", "bundle-close-after-stream"),
)
def test_envelope_bridge_revokes_bundle_replacement_or_close_mid_stream(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, bundle_path = _persisted(tmp_path / "store", assembled)
    inventory = _inventory(bundle_path)
    storage = next(
        item["storage"]
        for item in inventory["artifacts"]  # type: ignore[union-attr]
        if item["path"] == resolved.mutation_request.request_artifact
    )
    record = bundle_path / storage
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    bundle = store.reopen(digest)
    fired = False

    def revoke(stage: str, logical_path: str) -> None:
        nonlocal fired
        if (
            attack == "bundle-close-after-stream"
            and stage == "envelope-before-link"
            and not fired
        ):
            fired = True
            bundle.close()
            return
        if (
            stage == "chunk-before-visible-yield"
            and logical_path == resolved.mutation_request.request_artifact
            and not fired
        ):
            fired = True
            if attack == "record-replacement":
                _replace_record(record, record.read_bytes())
            else:
                bundle.close()

    monkeypatch.setattr(subject, "_stability_hook", revoke)
    with pytest.raises(
        ReleaseArtifactStoreV2Error,
        match="changed|revoked|closed|link count",
    ):
        bundle.write_private_mutation_envelope(
            target,
            resolved_request=resolved,
            transaction=journal.current,
        )
    assert fired
    assert not target.exists()
    assert tuple(target_dir.iterdir()) == ()
    bundle.close()
    store.close()


def test_envelope_bridge_retries_partial_writes_and_cleans_zero_write(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    journal, resolved, payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    original_write = os.write

    def short_write(descriptor: int, body: bytes) -> int:
        return original_write(descriptor, body[: min(3, len(body))])

    monkeypatch.setattr(subject.os, "write", short_write)
    with store.reopen(digest) as bundle:
        metadata = bundle.write_private_mutation_envelope(
            target_dir / "short.envelope",
            resolved_request=resolved,
            transaction=journal.current,
        )
    assert metadata.request_artifact_sha256 == hashlib.sha256(payload).hexdigest()

    calls = 0

    def zero_write(descriptor: int, body: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            return 0
        return original_write(descriptor, body)

    monkeypatch.setattr(subject.os, "write", zero_write)
    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="progress|write"):
            bundle.write_private_mutation_envelope(
                target_dir / "zero.envelope",
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert not (target_dir / "zero.envelope").exists()
    assert tuple(target_dir.glob(".zero.envelope.*.tmp")) == ()
    store.close()


@pytest.mark.parametrize("failure_stage", ("file", "directory"))
def test_envelope_bridge_fsync_failure_is_not_published_and_is_cleaned(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    original_fsync = os.fsync
    armed = False
    failed = False

    def arm(stage: str, _path: str) -> None:
        nonlocal armed
        expected = (
            "envelope-before-file-fsync"
            if failure_stage == "file"
            else "envelope-before-directory-fsync"
        )
        if stage == expected:
            armed = True

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if armed and not failed:
            failed = True
            raise OSError(errno.EIO, "synthetic envelope fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(subject, "_stability_hook", arm)
    monkeypatch.setattr(subject.os, "fsync", fail_once)
    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="persist|fsync|durable"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert failed
    assert not target.exists()
    assert tuple(target_dir.iterdir()) == ()
    store.close()


@pytest.mark.parametrize("attack", ("file", "symlink", "hardlink"))
def test_envelope_bridge_no_clobber_preserves_every_existing_target_type(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    outside = tmp_path / "outside"
    outside.write_bytes(b"do-not-clobber")
    if attack == "file":
        target.write_bytes(b"existing")
    elif attack == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    before = os.lstat(target)

    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="exists|collision"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )
    after = os.lstat(target)
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert tuple(target_dir.glob(f".{target.name}.*.tmp")) == ()
    store.close()


@pytest.mark.parametrize("attack", ("truncate", "trailing"))
def test_envelope_bridge_rejects_truncated_or_oversized_pinned_record(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
) -> None:
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, bundle_path = _persisted(tmp_path / "store", assembled)
    inventory = _inventory(bundle_path)
    storage = next(
        item["storage"]
        for item in inventory["artifacts"]  # type: ignore[union-attr]
        if item["path"] == resolved.mutation_request.request_artifact
    )
    record = bundle_path / storage
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    bundle = store.reopen(digest)
    body = record.read_bytes()
    _rewrite_record(record, body[:-1] if attack == "truncate" else body + b"x")

    with pytest.raises(ReleaseArtifactStoreV2Error, match="size|digest|truncated"):
        bundle.write_private_mutation_envelope(
            target,
            resolved_request=resolved,
            transaction=journal.current,
        )
    assert not target.exists()
    assert tuple(target_dir.iterdir()) == ()
    bundle.close()
    store.close()


def test_envelope_bridge_detects_reserved_field_split_across_stream_chunks(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    prefix = b"x" * (65536 - len(b"operation"))
    hostile = prefix + b"operation" + b"Sha256" + b"tail"
    hostile_assembled = _assembled_with_mutation_payload(assembled, hostile)
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", hostile_assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", hostile_assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"

    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="reserved"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert not target.exists()
    assert tuple(target_dir.iterdir()) == ()
    store.close()


def test_envelope_bridge_rejects_symlink_target_directory(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="directory"):
            bundle.write_private_mutation_envelope(
                linked / "mutation.envelope",
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert tuple(real.iterdir()) == ()
    store.close()


def test_envelope_bridge_rejects_non_owner_only_directory_and_stale_stage(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
) -> None:
    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    target_dir.chmod(0o750)
    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="owner-only"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )

    target_dir.chmod(0o700)
    stale = target_dir / f".{target.name}.{'a' * 32}.tmp"
    stale.write_bytes(b"partial")
    stale.chmod(0o600)
    inode = stale.stat().st_ino
    with store.reopen(digest) as bundle:
        with pytest.raises(ReleaseArtifactStoreV2Error, match="unresolved staging"):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert stale.stat().st_ino == inode
    assert stale.read_bytes() == b"partial"
    assert not target.exists()
    store.close()


@pytest.mark.parametrize(
    "attack",
    ("fanout", "prelink-fanout", "replace-target", "replace-directory"),
)
def test_envelope_bridge_filesystem_races_fail_closed_without_clobber(
    tmp_path: Path,
    assembled: AssembledReleasePlanV2,
    attack: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_tools.release_artifact_store_v2 as subject

    journal, resolved, _payload = _uncertain_current_mutation(
        tmp_path / "journal", assembled
    )
    store, digest, _ = _persisted(tmp_path / "store", assembled)
    target_dir = tmp_path / "private"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "mutation.envelope"
    outside = tmp_path / "outside-link"
    moved = tmp_path / "moved-private"
    fired = False

    def race(stage: str, _path: str) -> None:
        nonlocal fired
        wanted = (
            "envelope-after-link"
            if attack in {"fanout", "replace-target"}
            else "envelope-before-link"
        )
        if stage != wanted or fired:
            return
        fired = True
        if attack == "fanout":
            os.link(target, outside)
        elif attack == "prelink-fanout":
            staged = tuple(target_dir.glob(f".{target.name}.*.tmp"))
            assert len(staged) == 1
            os.link(staged[0], target)
        elif attack == "replace-target":
            target.unlink()
            target.write_bytes(b"hostile-replacement")
            target.chmod(0o600)
        else:
            target_dir.rename(moved)
            target_dir.mkdir(mode=0o700)

    monkeypatch.setattr(subject, "_stability_hook", race)
    with store.reopen(digest) as bundle:
        with pytest.raises(
            ReleaseArtifactStoreV2Error,
            match="directory|replaced|link|exists",
        ):
            bundle.write_private_mutation_envelope(
                target,
                resolved_request=resolved,
                transaction=journal.current,
            )
    assert fired
    if attack == "replace-target":
        assert target.read_bytes() == b"hostile-replacement"
    else:
        assert not target.exists()
    assert tuple(target_dir.glob(f".{target.name}.*.tmp")) == ()
    if moved.exists():
        assert tuple(moved.glob(f".{target.name}.*.tmp")) == ()
    store.close()
