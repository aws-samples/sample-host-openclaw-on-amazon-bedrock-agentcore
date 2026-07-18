from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from release_tools.contracts import (
    StagingTransactionV1,
    canonical_json_bytes,
    write_new_contract,
)
from release_tools.transaction import TransactionJournal


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "staging-release.py"
ACCOUNT = "123456789012"
REGION = "eu-west-1"


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"], cwd=repository, check=True
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
    ).strip()

    call_log = tmp_path / "calls.log"
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "aws",
        """#!/bin/bash
set -eu
printf 'aws <%s>\n' "$*" >> "$RELEASE_CALL_LOG"
if [[ "$*" == "sts get-caller-identity --query Account --output text --region eu-west-1" ]]; then
  printf '%s\n' "$RELEASE_TEST_ACCOUNT"
else
  printf 'unexpected aws command: %s\n' "$*" >&2
  exit 97
fi
""",
    )
    driver = tmp_path / "phase-driver"
    _write_executable(
        driver,
        """#!/bin/bash
set -eu
phase=""
while (($#)); do
  case "$1" in
    --phase) phase="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'driver <%s>\n' "$phase" >> "$RELEASE_CALL_LOG"
if [[ "${RELEASE_FAIL_PHASE:-}" == "$phase" ]]; then
  exit 75
fi
case "$phase" in
  image) printf '{"runtime_image_digest":"sha256:%064d"}\n' 0 ;;
  runtime) printf '{"runtime_id":"Runtime-ABCDEFGHIJ","runtime_version":"7"}\n' ;;
  context) printf '{"runtime_context_sha256":"%064d"}\n' 1 ;;
  rollback) printf '{"rollback_reference":"%s"}\n' "$RELEASE_ROLLBACK_REFERENCE" ;;
  *) printf '{}\n' ;;
esac
""",
    )
    journal = tmp_path / "journal.json"
    rollback = (
        f"rollback:v1:{ACCOUNT}:{REGION}:{commit}:sha256:" + "9" * 64
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "RELEASE_CALL_LOG": str(call_log),
        "RELEASE_TEST_ACCOUNT": ACCOUNT,
        "RELEASE_ROLLBACK_REFERENCE": rollback,
        "AWS_ACCESS_KEY_ID": "poison",
        "AWS_SECRET_ACCESS_KEY": "poison",
        "AWS_SESSION_TOKEN": "poison",
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "poison-token"),
        "AWS_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/poison",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9/poison",
    }
    return {
        "repo": repository,
        "commit": commit,
        "tree": tree,
        "journal": journal,
        "driver": driver,
        "log": call_log,
        "rollback": rollback,
        "env": env,
    }


def _run(
    fixture: dict[str, object],
    *arguments: str,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    env = {**fixture["env"], **environment}
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(fixture["repo"]),
            "--account",
            ACCOUNT,
            "--region",
            REGION,
            "--commit",
            str(fixture["commit"]),
            *arguments,
        ],
        cwd=fixture["repo"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _preflight(fixture: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--preflight",
    )


def _phase(
    fixture: dict[str, object],
    phase: str,
    *,
    confirmation: str | None = None,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    transaction_id = f"release_{fixture['commit']}"
    args = [
        "--journal",
        str(fixture["journal"]),
        "--phase",
        phase,
        "--driver",
        str(fixture["driver"]),
        "--rollback-reference",
        str(fixture["rollback"]),
    ]
    if confirmation is not None:
        args.extend(["--confirm", confirmation])
    elif phase:
        args.extend(["--confirm", f"mutate:{transaction_id}:{phase}"])
    return _run(fixture, *args, **environment)


def test_help_exposes_only_the_explicit_release_modes() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    for option in (
        "--preflight",
        "--phase",
        "--resume",
        "--status",
        "--rollback",
    ):
        assert option in completed.stdout
    assert "agentcore deploy" not in completed.stdout.casefold()


def test_preflight_and_status_never_discover_aws_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    preflight = _preflight(fixture)
    status = _run(fixture, "--status", str(fixture["journal"]))

    assert preflight.returncode == 0, preflight.stderr
    assert status.returncode == 0, status.stderr
    assert not fixture["log"].exists()
    current = StagingTransactionV1.from_bytes(status.stdout.encode("utf-8"))
    assert current.state == "PREFLIGHTED"


def test_mutation_requires_exact_confirmation_before_credentials_or_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(
        fixture,
        "foundation",
        confirmation="mutate:wrong:foundation",
    )

    assert completed.returncode != 0
    assert "confirmation" in completed.stderr.casefold()
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_confirmed_phase_discovers_exact_account_immediately_before_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation")

    assert completed.returncode == 0, completed.stderr
    assert fixture["log"].read_text(encoding="utf-8").splitlines() == [
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
        "driver <foundation>",
    ]
    assert TransactionJournal.load(fixture["journal"]).current.state == (
        "FOUNDATION_READY"
    )


def test_post_dispatch_failure_stays_uncertain_and_blocks_later_phases(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    fixture["log"].unlink()

    failed = _phase(fixture, "image", RELEASE_FAIL_PHASE="image")
    after_failure = TransactionJournal.load(fixture["journal"]).current
    later = _phase(fixture, "runtime")
    resume = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"mutate:release_{fixture['commit']}:image",
    )

    assert failed.returncode != 0
    assert after_failure.state == "UNCERTAIN"
    assert after_failure.last_stable_state == "FOUNDATION_READY"
    assert after_failure.uncertain_phase == "IMAGE_PUBLISHED"
    assert later.returncode != 0
    assert resume.returncode != 0
    assert "reconcile" in resume.stderr.casefold()
    assert fixture["log"].read_text(encoding="utf-8").splitlines().count(
        "driver <image>"
    ) == 1
    assert "driver <runtime>" not in fixture["log"].read_text(encoding="utf-8")


def test_explicit_absent_reconciliation_allows_safe_resume(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0

    reconciled = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "absent",
    )
    resumed = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"mutate:release_{fixture['commit']}:image",
    )

    assert reconciled.returncode == 0, reconciled.stderr
    assert resumed.returncode == 0, resumed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "IMAGE_PUBLISHED"
    assert current.runtime_image_digest == "sha256:" + "0" * 64


def test_verified_rollback_is_write_ahead_and_never_exposes_endpoint_retarget(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    verified = StagingTransactionV1.from_mapping(
        {
            "schema": StagingTransactionV1.SCHEMA,
            "transactionId": f"release_{fixture['commit']}",
            "sourceCommit": fixture["commit"],
            "sourceTree": fixture["tree"],
            "account": ACCOUNT,
            "region": REGION,
            "state": "VERIFIED",
            "lastStableState": "VERIFIED",
            "revision": 9,
            "runtimeImageDigest": "sha256:" + "0" * 64,
            "runtimeId": "Runtime-ABCDEFGHIJ",
            "runtimeVersion": "7",
            "runtimeEndpointName": f"release_{fixture['commit']}",
            "runtimeContextSha256": "1" * 64,
            "rollbackReference": fixture["rollback"],
            "uncertainPhase": "",
        }
    )
    write_new_contract(fixture["journal"], verified)
    transaction_id = f"release_{fixture['commit']}"

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--rollback",
        transaction_id,
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"rollback:{transaction_id}",
    )

    assert completed.returncode == 0, completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "ROLLED_BACK"
    assert fixture["log"].read_text(encoding="utf-8").endswith(
        "driver <rollback>\n"
    )
    source = SCRIPT.read_text(encoding="utf-8")
    assert "update-agent-runtime-endpoint" not in source
    assert "agentcore deploy" not in source.casefold()


def test_status_rejects_noncanonical_journal_without_aws_access(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["journal"].write_bytes(canonical_json_bytes({"schema": "wrong"}))

    completed = _run(fixture, "--status", str(fixture["journal"]))

    assert completed.returncode != 0
    assert not fixture["log"].exists()
