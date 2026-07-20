"""Retirement of the legacy driver/composer completion route.

These tests pin the operator entrypoint so that no operator-reachable path can
complete a mutating release through a `--driver` stdout acknowledgement.  The
accepted authority is the v2 release session; until it exposes a reviewed
operator command the operator entrypoint must fail closed on every legacy
mutation flag, while credential-free preflight/status stay available.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from release_tools import cli as release_cli
from release_tools.transaction import TransactionJournal


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "staging-release.py"
DEPLOY = ROOT / "scripts" / "deploy.sh"


@pytest.mark.parametrize(
    "mode",
    [
        ["--phase", "foundation", "--journal", "/tmp/v1-release.json"],
        ["--resume", "/tmp/v1-release.json"],
        [
            "--rollback",
            "release_" + "a" * 40,
            "--journal",
            "/tmp/v1-release.json",
        ],
        ["--reconcile", "--resume", "/tmp/v1-release.json"],
    ],
)
def test_operator_entrypoint_refuses_every_legacy_completion_mode_before_io(
    mode: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        release_cli,
        "_assert_executing_repository",
        lambda root: (_ for _ in ()).throw(
            AssertionError(
                "legacy completion must stop before checkout or AWS access"
            )
        ),
    )

    result = release_cli.main(
        [*mode, "--root", str(ROOT)],
        production_site_packages=tmp_path,
    )

    assert result == 1
    assert "mutation is disabled" in capsys.readouterr().err.casefold()


def test_operator_entrypoint_refuses_a_driver_even_beside_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        release_cli,
        "_assert_executing_repository",
        lambda root: (_ for _ in ()).throw(
            AssertionError(
                "a driver must never reach preflight at the operator entrypoint"
            )
        ),
    )
    driver = tmp_path / "phase-driver"
    driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    driver.chmod(0o755)

    result = release_cli.main(
        [
            "--preflight",
            "--journal",
            str(tmp_path / "journal.json"),
            "--driver",
            str(driver),
            "--root",
            str(ROOT),
        ],
        production_site_packages=tmp_path,
    )

    assert result == 1
    assert "mutation is disabled" in capsys.readouterr().err.casefold()
    assert not (tmp_path / "journal.json").exists()


def test_deploy_shim_refuses_a_legacy_mutation_flag(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPT),
            "--phase",
            "foundation",
            "--journal",
            str(tmp_path / "journal.json"),
            "--root",
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "mutation is disabled" in completed.stderr.casefold()
    assert not (tmp_path / "journal.json").exists()


def test_deploy_sh_has_no_legacy_driver_completion_route() -> None:
    text = DEPLOY.read_text(encoding="utf-8")

    assert "--driver" not in text
    assert "--phase" not in text
    assert "PHASE_TO_STATE" not in text


def test_ambiguous_live_observation_stays_uncertain_never_success(
    tmp_path: Path,
) -> None:
    from release_tools.test_cli import _fixture, _phase, _preflight

    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(
        fixture,
        "foundation",
        RELEASE_FAIL_OBSERVE_PHASE="foundation",
    )

    assert completed.returncode != 0
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.state != "FOUNDATION_READY"
