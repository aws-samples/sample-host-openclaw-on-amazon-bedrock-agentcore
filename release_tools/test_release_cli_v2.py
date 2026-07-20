from __future__ import annotations

from pathlib import Path

import pytest

from release_tools.release_session_v2 import (
    ReleaseSessionResultV2,
    ReleaseSessionV2Error,
)


def _result(*, state: str, action: str, revision: int) -> ReleaseSessionResultV2:
    return ReleaseSessionResultV2(
        plan_sha256="a" * 64,
        source_commit="b" * 40,
        source_tree="c" * 40,
        account="123456789012",
        region="eu-west-1",
        state=state,
        revision=revision,
        completed_step_count=0,
        total_step_count=5,
        step_result={
            "stepId": "foundation-baseline",
            "phase": "foundation",
            "kind": "BASELINE_OBSERVE",
            "action": action,
        },
    )


STABLE_RESULT = _result(state="PREFLIGHTED", action="OBSERVED_READ_ONLY", revision=1)
UNCERTAIN_RESULT = _result(
    state="UNCERTAIN", action="DISPATCHED_UNCERTAIN", revision=2
)


def _session(
    *,
    status_result: ReleaseSessionResultV2 | None = STABLE_RESULT,
    run_result: ReleaseSessionResultV2 | None = STABLE_RESULT,
    status_error: Exception | None = None,
    run_error: Exception | None = None,
):
    events: list[tuple] = []

    class FakeSession:
        @classmethod
        def status(cls, root: Path, *, expected_plan_sha256: str):
            events.append(("status", root, expected_plan_sha256))
            if status_error is not None:
                raise status_error
            return status_result

        @classmethod
        def run_one(
            cls,
            root: Path,
            *,
            expected_plan_sha256: str,
            site_packages: Path,
            aws_directory: Path,
        ):
            events.append(
                (
                    "run_one",
                    root,
                    expected_plan_sha256,
                    site_packages,
                    aws_directory,
                )
            )
            if run_error is not None:
                raise run_error
            return run_result

    return FakeSession, events


def _session_root(tmp_path: Path) -> Path:
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    return root


def test_status_is_credential_free_and_causes_no_mutation(
    tmp_path: Path, capsysbinary
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    session, events = _session()

    exit_code = main(
        ["--status", "--root", str(root), "--expected-plan-sha256", "a" * 64],
        session_factory=session,
    )

    assert exit_code == 0
    assert [name for name, *_ in events] == ["status"]
    assert capsysbinary.readouterr().out == STABLE_RESULT.to_bytes()


def test_preflight_is_credential_free_and_never_dispatches(
    tmp_path: Path, capsysbinary
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    session, events = _session()

    exit_code = main(
        ["--preflight", "--root", str(root), "--expected-plan-sha256", "a" * 64],
        session_factory=session,
    )

    assert exit_code == 0
    assert [name for name, *_ in events] == ["status"]
    assert "run_one" not in [name for name, *_ in events]


def test_credential_free_modes_need_no_site_packages(tmp_path: Path) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    session, events = _session()

    # production_site_packages deliberately omitted; status must still work.
    assert (
        main(
            ["--status", "--root", str(root), "--expected-plan-sha256", "a" * 64],
            session_factory=session,
        )
        == 0
    )
    assert [name for name, *_ in events] == ["status"]


def test_run_one_drives_exactly_one_step_and_emits_canonical_json(
    tmp_path: Path, capsysbinary
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()
    session, events = _session(run_result=STABLE_RESULT)

    exit_code = main(
        [
            "--run-one",
            "--root",
            str(root),
            "--expected-plan-sha256",
            "a" * 64,
            "--aws-directory",
            str(aws_directory),
        ],
        production_site_packages=site_packages,
        session_factory=session,
    )

    assert exit_code == 0
    run_calls = [event for event in events if event[0] == "run_one"]
    assert len(run_calls) == 1
    assert run_calls[0][3] == site_packages
    assert run_calls[0][4] == aws_directory
    assert run_calls[0][2] == "a" * 64
    assert capsysbinary.readouterr().out == STABLE_RESULT.to_bytes()


def test_run_one_uncertain_outcome_fails_closed_without_success(
    tmp_path: Path, capsysbinary
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()
    session, events = _session(run_result=UNCERTAIN_RESULT)

    exit_code = main(
        [
            "--run-one",
            "--root",
            str(root),
            "--expected-plan-sha256",
            "a" * 64,
            "--aws-directory",
            str(aws_directory),
        ],
        production_site_packages=site_packages,
        session_factory=session,
    )

    captured = capsysbinary.readouterr()
    assert exit_code != 0
    assert len([event for event in events if event[0] == "run_one"]) == 1
    assert captured.out == b""
    assert b"SUCCESS" not in captured.out
    assert UNCERTAIN_RESULT.to_bytes() not in captured.out


def test_run_one_session_error_fails_closed(tmp_path: Path, capsysbinary) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    site_packages = tmp_path / "site-packages"
    aws_directory = tmp_path / "aws"
    site_packages.mkdir()
    aws_directory.mkdir()
    session, events = _session(
        run_error=ReleaseSessionV2Error("release session step failed closed")
    )

    exit_code = main(
        [
            "--run-one",
            "--root",
            str(root),
            "--expected-plan-sha256",
            "a" * 64,
            "--aws-directory",
            str(aws_directory),
        ],
        production_site_packages=site_packages,
        session_factory=session,
    )

    assert exit_code != 0
    assert capsysbinary.readouterr().out == b""
    assert len([event for event in events if event[0] == "run_one"]) == 1


def test_malformed_plan_digest_fails_before_any_session_call(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    session, events = _session()

    exit_code = main(
        ["--status", "--root", str(root), "--expected-plan-sha256", "not-a-digest"],
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_missing_plan_digest_fails_before_any_session_call(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    session, events = _session()

    exit_code = main(
        ["--status", "--root", str(root)],
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_relative_root_fails_before_any_session_call(tmp_path: Path) -> None:
    from release_tools.release_cli_v2 import main

    session, events = _session()

    exit_code = main(
        ["--status", "--root", "relative/session", "--expected-plan-sha256", "a" * 64],
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_symlinked_root_is_rejected_before_any_session_call(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    target = tmp_path / "real-session"
    target.mkdir(mode=0o700)
    root = tmp_path / "session-link"
    root.symlink_to(target, target_is_directory=True)
    session, events = _session()

    exit_code = main(
        ["--status", "--root", str(root), "--expected-plan-sha256", "a" * 64],
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_run_one_without_site_packages_fails_before_session(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    aws_directory = tmp_path / "aws"
    aws_directory.mkdir()
    session, events = _session()

    exit_code = main(
        [
            "--run-one",
            "--root",
            str(root),
            "--expected-plan-sha256",
            "a" * 64,
            "--aws-directory",
            str(aws_directory),
        ],
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_run_one_without_aws_directory_fails_before_session(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    session, events = _session()

    exit_code = main(
        ["--run-one", "--root", str(root), "--expected-plan-sha256", "a" * 64],
        production_site_packages=site_packages,
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


def test_symlinked_aws_directory_is_rejected_before_session(
    tmp_path: Path,
) -> None:
    from release_tools.release_cli_v2 import main

    root = _session_root(tmp_path)
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    aws_target = tmp_path / "real-aws"
    aws_target.mkdir()
    aws_link = tmp_path / "aws-link"
    aws_link.symlink_to(aws_target, target_is_directory=True)
    session, events = _session()

    exit_code = main(
        [
            "--run-one",
            "--root",
            str(root),
            "--expected-plan-sha256",
            "a" * 64,
            "--aws-directory",
            str(aws_link),
        ],
        production_site_packages=site_packages,
        session_factory=session,
    )

    assert exit_code != 0
    assert events == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--status", "--run-one", "--root", "/tmp/x", "--expected-plan-sha256", "a" * 64],
    ],
)
def test_ambiguous_or_missing_mode_fails_closed(argv) -> None:
    from release_tools.release_cli_v2 import main

    session, events = _session()
    with pytest.raises(SystemExit) as caught:
        main(argv, session_factory=session)
    assert caught.value.code != 0
    assert events == []
