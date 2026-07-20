from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import inspect
import os
from pathlib import Path
import stat

import pytest

from release_tools.contracts import canonical_json_bytes, parse_canonical_object
from release_tools.release_plan_v2 import ReleasePlanAssemblerV2
from release_tools.release_runner_v2 import ReleaseRunnerStepResultV2
from release_tools.test_release_plan_v2 import _preclosed_source


@pytest.fixture(scope="module")
def assembled(tmp_path_factory: pytest.TempPathFactory):
    source = _preclosed_source(tmp_path_factory.mktemp("release-session-source"))
    return ReleasePlanAssemblerV2.assemble(source)


def _overwrite_read_only(path: Path, payload: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(payload)
    path.chmod(0o400)


def _fake_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str = "",
    on_init=None,
    on_run=None,
):
    import release_tools.release_session_v2 as session_module

    events: list[tuple[str, object]] = []

    class FakeAuthority:
        @classmethod
        @contextmanager
        def open_bootstrap(
            cls,
            plan,
            *,
            site_packages: Path,
            aws_directory: Path,
        ):
            events.append(("authority-open", plan.digest()))
            events.append(("site-packages", site_packages))
            events.append(("aws-directory", aws_directory))
            try:
                yield object()
            finally:
                events.append(("authority-close", plan.digest()))

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            events.append(("controller-init", tuple(sorted(kwargs))))
            if on_init is not None:
                on_init()
            self.journal = kwargs["journal"]

        def run_one(self) -> ReleaseRunnerStepResultV2:
            events.append(("run-one", self.journal.current.revision))
            if on_run is not None:
                on_run()
            if failure:
                raise RuntimeError(failure)
            step = self.journal.plan.steps[
                self.journal.current.completed_step_count
            ]
            if self.journal.current.state == "NEW":
                self.journal.advance_preflight()
            return ReleaseRunnerStepResultV2(
                step_id=step.step_id,
                phase=step.phase,
                kind=step.kind,
                provider="CLOUDFORMATION",
                action="OBSERVED_READ_ONLY",
                state=self.journal.current.state,
                revision=self.journal.current.revision,
            )

    monkeypatch.setattr(
        session_module, "AuthenticatedAwsAuthorityV2", FakeAuthority
    )
    monkeypatch.setattr(
        session_module, "AcceptedReleaseControllerV2", FakeController
    )
    return events


def test_initialize_returns_exact_new_status(
    tmp_path: Path, assembled
) -> None:
    from release_tools.release_session_v2 import AcceptedReleaseSessionV2

    result = AcceptedReleaseSessionV2.initialize(
        tmp_path / "session", assembled
    )

    assert result.plan_sha256 == assembled.plan.digest()
    assert result.state == "NEW"
    assert result.revision == 0
    assert result.completed_step_count == 0
    assert result.total_step_count == len(assembled.plan.steps)
    assert result.step_result is None


def test_initialize_is_one_owner_only_fixed_committed_namespace(
    tmp_path: Path, assembled
) -> None:
    from release_tools.release_session_v2 import AcceptedReleaseSessionV2

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert set(os.listdir(root)) == {
        ".journal.json.lock",
        "COMMITTED",
        "PLAN-BINDING.json",
        "artifacts",
        "envelopes",
        "evidence",
        "journal.json",
        "runtime-context",
        "scratch",
    }
    for name in (
        "artifacts",
        "envelopes",
        "evidence",
        "runtime-context",
        "scratch",
    ):
        details = (root / name).stat()
        assert stat.S_ISDIR(details.st_mode)
        assert details.st_uid == os.geteuid()
        assert stat.S_IMODE(details.st_mode) == 0o700
    for name in ("COMMITTED", "PLAN-BINDING.json"):
        details = (root / name).stat()
        assert stat.S_ISREG(details.st_mode)
        assert details.st_uid == os.geteuid()
        assert details.st_nlink == 1
        assert stat.S_IMODE(details.st_mode) == 0o400
    for name in ("journal.json", ".journal.json.lock"):
        details = (root / name).stat()
        assert stat.S_ISREG(details.st_mode)
        assert details.st_uid == os.geteuid()
        assert details.st_nlink == 1
        assert stat.S_IMODE(details.st_mode) == 0o600

    binding_payload = (root / "PLAN-BINDING.json").read_bytes()
    binding = parse_canonical_object(binding_payload)
    marker = parse_canonical_object((root / "COMMITTED").read_bytes())
    namespace_identity = binding.pop("namespaceIdentitySha256")
    assert isinstance(namespace_identity, str)
    assert len(namespace_identity) == 64
    int(namespace_identity, 16)
    assert binding == {
        "account": assembled.plan.account,
        "artifactCount": len(assembled.plan.artifacts),
        "planSha256": assembled.plan.digest(),
        "planSize": len(assembled.plan.to_bytes()),
        "region": assembled.plan.region,
        "schema": "personal-operator.release-session-binding.v2",
        "sourceCommit": assembled.plan.source_commit,
        "sourceTree": assembled.plan.source_tree,
        "stepCount": len(assembled.plan.steps),
        "transactionId": assembled.plan.transaction_id,
    }
    assert marker == {
        "bindingSha256": hashlib.sha256(
            binding_payload
        ).hexdigest(),
        "planSha256": assembled.plan.digest(),
        "schema": "personal-operator.release-session-commit.v2",
    }


def test_initialize_has_no_aws_or_controller_path(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    import release_tools.release_session_v2 as session_module

    class ForbiddenAuthority:
        @classmethod
        def open_bootstrap(cls, *_args: object, **_kwargs: object):
            raise AssertionError("AWS authentication reached initialization")

    class ForbiddenController:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("controller reached initialization")

    monkeypatch.setattr(
        session_module, "AuthenticatedAwsAuthorityV2", ForbiddenAuthority
    )
    monkeypatch.setattr(
        session_module, "AcceptedReleaseControllerV2", ForbiddenController
    )

    result = session_module.AcceptedReleaseSessionV2.initialize(
        tmp_path / "session", assembled
    )
    assert result.state == "NEW"


def test_initialize_never_commits_an_on_disk_journal_that_differs_from_memory(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    import release_tools.release_session_v2 as session_module

    root = tmp_path / "session"

    def corrupt_journal(stage: str) -> None:
        if stage == "initialize-before-commit":
            (root / "journal.json").write_bytes(b'{"schema":"crossed"}\n')

    monkeypatch.setattr(session_module, "_stability_hook", corrupt_journal)

    with pytest.raises(
        session_module.ReleaseSessionV2Error, match="could not be initialized"
    ):
        session_module.AcceptedReleaseSessionV2.initialize(root, assembled)

    assert not (root / "COMMITTED").exists()


def test_initialize_replay_fails_without_changing_the_exact_session(
    tmp_path: Path, assembled
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    before = AcceptedReleaseSessionV2.initialize(root, assembled).to_bytes()
    binding = (root / "PLAN-BINDING.json").read_bytes()

    with pytest.raises(
        ReleaseSessionV2Error, match="could not be initialized"
    ) as caught:
        AcceptedReleaseSessionV2.initialize(root, assembled)

    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert (root / "PLAN-BINDING.json").read_bytes() == binding
    assert (
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        ).to_bytes()
        == before
    )


@pytest.mark.parametrize("target", ("root", "artifacts", "evidence"))
def test_reopen_rejects_symlinked_session_namespace(
    tmp_path: Path, assembled, target: str
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    if target == "root":
        moved = tmp_path / "moved-session"
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    else:
        moved = root / f"moved-{target}"
        (root / target).rename(moved)
        (root / target).symlink_to(moved, target_is_directory=True)

    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )


@pytest.mark.parametrize("target", ("root", "scratch", "journal.json"))
def test_reopen_rejects_non_owner_only_session_entries(
    tmp_path: Path, assembled, target: str
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    selected = root if target == "root" else root / target
    selected.chmod(0o755 if selected.is_dir() else 0o644)

    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )


@pytest.mark.parametrize("marker", ("missing", "truncated", "crossed"))
def test_reopen_rejects_torn_or_crossed_commit_marker(
    tmp_path: Path, assembled, marker: str
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    path = root / "COMMITTED"
    if marker == "missing":
        path.unlink()
    elif marker == "truncated":
        _overwrite_read_only(path, b'{"schema":')
    else:
        value = parse_canonical_object(path.read_bytes())
        value["planSha256"] = "f" * 64
        _overwrite_read_only(path, canonical_json_bytes(value))

    with pytest.raises(
        ReleaseSessionV2Error, match="could not be reopened"
    ) as caught:
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_reopen_rejects_expected_or_retained_plan_substitution(
    tmp_path: Path, assembled
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256="f" * 64
        )

    binding_path = root / "PLAN-BINDING.json"
    marker_path = root / "COMMITTED"
    binding = parse_canonical_object(binding_path.read_bytes())
    binding["planSha256"] = "f" * 64
    binding_payload = canonical_json_bytes(binding)
    marker = parse_canonical_object(marker_path.read_bytes())
    marker["planSha256"] = "f" * 64
    marker["bindingSha256"] = hashlib.sha256(binding_payload).hexdigest()
    _overwrite_read_only(binding_path, binding_payload)
    _overwrite_read_only(marker_path, canonical_json_bytes(marker))

    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )


def test_run_rejects_plan_substitution_before_authentication(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    events = _fake_execution(monkeypatch)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()

    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.run_one(
            root,
            expected_plan_sha256="f" * 64,
            site_packages=site_packages,
            aws_directory=aws_directory,
        )

    assert events == []


def test_reopen_detects_root_identity_replacement_before_use(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    import release_tools.release_session_v2 as session_module

    root = tmp_path / "session"
    session_module.AcceptedReleaseSessionV2.initialize(root, assembled)
    moved = tmp_path / "original-session"

    def replace_root(stage: str) -> None:
        if stage == "open-after-lock":
            root.rename(moved)
            root.mkdir(mode=0o700)

    monkeypatch.setattr(session_module, "_stability_hook", replace_root)

    with pytest.raises(
        session_module.ReleaseSessionV2Error, match="could not be reopened"
    ):
        session_module.AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )


def test_restart_runs_exactly_one_closed_controller_step_and_redacts_result(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_session_v2 import AcceptedReleaseSessionV2

    root = tmp_path / "session"
    initialized = AcceptedReleaseSessionV2.initialize(root, assembled)
    assert AcceptedReleaseSessionV2.from_bytes(initialized.to_bytes()) == initialized
    events = _fake_execution(monkeypatch)
    secret_site = tmp_path / "site-packages-AKIA-DO-NOT-RETURN"
    secret_aws = tmp_path / "aws-secret-DO-NOT-RETURN"
    secret_site.mkdir()
    secret_aws.mkdir()

    result = AcceptedReleaseSessionV2.run_one(
        root,
        expected_plan_sha256=assembled.plan.digest(),
        site_packages=secret_site,
        aws_directory=secret_aws,
    )

    assert [name for name, _value in events].count("authority-open") == 1
    assert [name for name, _value in events].count("controller-init") == 1
    assert [name for name, _value in events].count("run-one") == 1
    assert [name for name, _value in events].count("authority-close") == 1
    assert events.index(("authority-open", assembled.plan.digest())) < events.index(
        ("authority-close", assembled.plan.digest())
    )
    assert result.plan_sha256 == assembled.plan.digest()
    assert result.state == "PREFLIGHTED"
    assert result.revision == 1
    assert result.step_result == {
        "action": "OBSERVED_READ_ONLY",
        "kind": assembled.plan.steps[0].kind,
        "phase": assembled.plan.steps[0].phase,
        "stepId": assembled.plan.steps[0].step_id,
    }
    payload = result.to_bytes()
    assert AcceptedReleaseSessionV2.from_bytes(payload) == result
    assert b"AKIA" not in payload
    assert b"secret" not in payload.lower()
    assert b"site-packages" not in payload
    assert b"aws-directory" not in payload

    restarted = AcceptedReleaseSessionV2.status(
        root, expected_plan_sha256=assembled.plan.digest()
    )
    assert restarted.step_result is None
    assert restarted.plan_sha256 == result.plan_sha256
    assert restarted.state == result.state
    assert restarted.revision == result.revision
    assert restarted.state != initialized.state
    assert restarted.revision != initialized.revision


def test_run_rejects_and_permanently_detects_fixed_directory_replacement(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)

    def replace_scratch() -> None:
        (root / "scratch").rename(tmp_path / "original-scratch")
        (root / "scratch").mkdir(mode=0o700)

    _fake_execution(monkeypatch, on_run=replace_scratch)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()

    with pytest.raises(ReleaseSessionV2Error):
        AcceptedReleaseSessionV2.run_one(
            root,
            expected_plan_sha256=assembled.plan.digest(),
            site_packages=site_packages,
            aws_directory=aws_directory,
        )
    with pytest.raises(ReleaseSessionV2Error, match="could not be reopened"):
        AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )


def test_controller_construction_replacement_is_rejected_before_run_one(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)

    def replace_scratch() -> None:
        (root / "scratch").rename(tmp_path / "original-scratch")
        (root / "scratch").mkdir(mode=0o700)

    events = _fake_execution(monkeypatch, on_init=replace_scratch)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()

    with pytest.raises(ReleaseSessionV2Error):
        AcceptedReleaseSessionV2.run_one(
            root,
            expected_plan_sha256=assembled.plan.digest(),
            site_packages=site_packages,
            aws_directory=aws_directory,
        )

    assert [name for name, _value in events].count("controller-init") == 1
    assert [name for name, _value in events].count("run-one") == 0


def test_late_reopen_failure_closes_every_retained_session_capability(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    import release_tools.release_session_v2 as session_module

    root = tmp_path / "session"
    session_module.AcceptedReleaseSessionV2.initialize(root, assembled)
    original = session_module._assert_retained_directories
    calls = 0

    def fail_after_journal_open(
        root_fd: int, retained: object, expected: object
    ) -> None:
        nonlocal calls
        calls += 1
        original(root_fd, retained, expected)
        if calls == 1:
            raise session_module._SessionBoundaryError(
                "late exact-namespace failure"
            )

    monkeypatch.setattr(
        session_module, "_assert_retained_directories", fail_after_journal_open
    )

    with pytest.raises(
        session_module.ReleaseSessionV2Error, match="could not be reopened"
    ):
        session_module.AcceptedReleaseSessionV2.status(
            root, expected_plan_sha256=assembled.plan.digest()
        )

    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def test_run_failure_closes_authority_and_never_returns_exception_secrets(
    tmp_path: Path, assembled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    root = tmp_path / "session"
    AcceptedReleaseSessionV2.initialize(root, assembled)
    events = _fake_execution(
        monkeypatch,
        failure="AKIAIOSFODNN7EXAMPLE password=do-not-return",
    )
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()

    with pytest.raises(ReleaseSessionV2Error) as caught:
        AcceptedReleaseSessionV2.run_one(
            root,
            expected_plan_sha256=assembled.plan.digest(),
            site_packages=site_packages,
            aws_directory=aws_directory,
        )

    assert str(caught.value) == "release session step failed closed"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert [name for name, _value in events].count("authority-close") == 1


def test_outer_run_failure_discards_secret_bearing_filesystem_context(
    tmp_path: Path,
) -> None:
    from release_tools.release_session_v2 import (
        AcceptedReleaseSessionV2,
        ReleaseSessionV2Error,
    )

    missing = tmp_path / "session-AKIA-DO-NOT-RETURN"
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()

    with pytest.raises(ReleaseSessionV2Error) as caught:
        AcceptedReleaseSessionV2.run_one(
            missing,
            expected_plan_sha256="f" * 64,
            site_packages=site_packages,
            aws_directory=aws_directory,
        )

    assert str(caught.value) == "release session could not be reopened"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_session_public_api_has_no_injected_effect_or_evidence_boundary() -> None:
    from release_tools.release_session_v2 import AcceptedReleaseSessionV2

    assert tuple(inspect.signature(AcceptedReleaseSessionV2.initialize).parameters) == (
        "root",
        "assembled",
    )
    assert tuple(inspect.signature(AcceptedReleaseSessionV2.status).parameters) == (
        "root",
        "expected_plan_sha256",
    )
    assert tuple(inspect.signature(AcceptedReleaseSessionV2.run_one).parameters) == (
        "root",
        "expected_plan_sha256",
        "site_packages",
        "aws_directory",
    )
    source = (
        Path(__file__).with_name("release_session_v2.py").read_text(
            encoding="utf-8"
        )
    )
    for forbidden in (
        "subprocess",
        "importlib",
        "boto3",
        "botocore",
        "operator_driver",
        "provider_observation",
        "sdk_factory",
        "plugin",
        "print(",
        "logging",
    ):
        assert forbidden not in source
    assert source.count(".run_one()") == 1
    assert source.count(".open_bootstrap(") == 1
