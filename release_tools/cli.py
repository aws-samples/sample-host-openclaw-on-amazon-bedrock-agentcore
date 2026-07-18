"""Credential-lazy command surface for the immutable staging transaction."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

from release_tools.contracts import (
    ContractError,
    StagingTransactionV1,
    parse_canonical_object,
    read_regular_bytes,
)
from release_tools.transaction import TransactionError, TransactionJournal


REQUIRED_REGION = "eu-west-1"
PHASE_TO_STATE = {
    "foundation": "FOUNDATION_READY",
    "image": "IMAGE_PUBLISHED",
    "runtime": "RUNTIME_READY",
    "endpoint": "ENDPOINT_READY",
    "context": "CONTEXT_WRITTEN",
    "consumer-changesets": "CONSUMER_CHANGESETS_READY",
    "consumers": "CONSUMERS_APPLIED",
    "verify": "VERIFIED",
}
STATE_TO_PHASE = {state: phase for phase, state in PHASE_TO_STATE.items()}
PHASE_EVIDENCE_FIELDS = {
    "foundation": set(),
    "image": {"runtime_image_digest"},
    "runtime": {"runtime_id", "runtime_version"},
    "endpoint": set(),
    "context": {"runtime_context_sha256"},
    "consumer-changesets": set(),
    "consumers": set(),
    "verify": set(),
}

_ACCOUNT = re.compile(r"[0-9]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class ReleaseCliError(RuntimeError):
    """The requested release operation is unsafe or incomplete."""


def _git(root: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseCliError(
            f"cannot resolve Git release identity: {' '.join(arguments)}"
        ) from error


def _validate_region(region: str) -> None:
    if region != REQUIRED_REGION:
        raise ReleaseCliError(
            f"release region must be exactly {REQUIRED_REGION}"
        )
    for name in ("CDK_DEFAULT_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        configured = os.environ.get(name)
        if configured and configured != region:
            raise ReleaseCliError(
                f"{name} must be exactly {region}; got {configured}"
            )


def _preflight_identity(args: argparse.Namespace) -> tuple[Path, str, str, str]:
    try:
        root = Path(args.root).resolve(strict=True)
    except OSError as error:
        raise ReleaseCliError("release root does not exist") from error
    if not root.is_dir():
        raise ReleaseCliError("release root is not a directory")
    account = args.account or os.environ.get("PERSONAL_OPERATOR_RELEASE_ACCOUNT", "")
    if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
        raise ReleaseCliError("release account must be a non-synthetic 12-digit ID")
    region = args.region
    _validate_region(region)
    head = _git(root, "rev-parse", "HEAD")
    commit = args.commit or os.environ.get("PERSONAL_OPERATOR_RELEASE_COMMIT", "")
    if _COMMIT.fullmatch(commit) is None or commit != head:
        raise ReleaseCliError("release commit must equal the exact Git HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if args.tree and args.tree != tree:
        raise ReleaseCliError("release tree differs from the exact Git tree")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseCliError("release preflight requires a clean worktree")
    return root, account, commit, tree


def _emit(transaction: StagingTransactionV1) -> None:
    sys.stdout.buffer.write(transaction.to_bytes())


def _journal_path(args: argparse.Namespace) -> Path:
    if args.journal is None:
        raise ReleaseCliError("--journal is required for this mode")
    return Path(args.journal)


def _check_requested_identity(
    current: StagingTransactionV1, args: argparse.Namespace
) -> None:
    if args.account and args.account != current.account:
        raise ReleaseCliError("requested account differs from the journal")
    if args.region != current.region:
        raise ReleaseCliError("requested region differs from the journal")
    if args.commit and args.commit != current.source_commit:
        raise ReleaseCliError("requested commit differs from the journal")
    if args.tree and args.tree != current.source_tree:
        raise ReleaseCliError("requested tree differs from the journal")


def _driver(args: argparse.Namespace) -> Path:
    if args.driver is None:
        raise ReleaseCliError(
            "--driver is required at an explicitly confirmed mutation boundary"
        )
    candidate = Path(args.driver)
    if candidate.is_symlink():
        raise ReleaseCliError("release phase driver must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ReleaseCliError("release phase driver does not exist") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ReleaseCliError("release phase driver is not executable")
    return resolved


def _discover_account(expected_account: str, region: str) -> None:
    """Touch credentials only after durable intent and immediately pre-dispatch."""

    try:
        completed = subprocess.run(
            [
                "aws",
                "sts",
                "get-caller-identity",
                "--query",
                "Account",
                "--output",
                "text",
                "--region",
                region,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReleaseCliError("AWS credential discovery is unavailable") from error
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != expected_account:
        raise ReleaseCliError(
            "authenticated AWS account differs from the release journal"
        )


def _invoke_driver(
    driver: Path,
    *,
    phase: str,
    journal: TransactionJournal,
) -> dict[str, Any]:
    current = journal.current
    completed = subprocess.run(
        [
            str(driver),
            "--phase",
            phase,
            "--journal",
            str(journal.path),
            "--transaction-id",
            current.transaction_id,
            "--source-commit",
            current.source_commit,
            "--source-tree",
            current.source_tree,
            "--account",
            current.account,
            "--region",
            current.region,
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseCliError(
            f"{phase} mutation did not return authoritative evidence"
            + (f": {detail}" if detail else "")
        )
    try:
        evidence = parse_canonical_object(completed.stdout)
    except ContractError as error:
        raise ReleaseCliError(
            f"{phase} driver returned noncanonical evidence"
        ) from error
    return evidence


def _phase_evidence(phase: str, raw: Mapping[str, Any]) -> dict[str, str]:
    expected = PHASE_EVIDENCE_FIELDS[phase]
    if set(raw) != expected:
        raise ReleaseCliError(
            f"{phase} driver evidence has the wrong fields"
        )
    if any(not isinstance(value, str) for value in raw.values()):
        raise ReleaseCliError(f"{phase} driver evidence must contain strings")
    return dict(raw)


def _rollback_reference(
    journal: TransactionJournal, args: argparse.Namespace
) -> str:
    supplied = args.rollback_reference or ""
    recorded = journal.current.rollback_reference
    if recorded and supplied and supplied != recorded:
        raise ReleaseCliError("rollback reference differs from the journal")
    reference = recorded or supplied
    if not reference:
        raise ReleaseCliError(
            "the first cloud phase requires an exact --rollback-reference"
        )
    return reference


def _run_phase(
    journal: TransactionJournal,
    phase: str,
    args: argparse.Namespace,
) -> StagingTransactionV1:
    target = PHASE_TO_STATE[phase]
    if journal.resume_target() != target:
        raise ReleaseCliError(
            f"{phase} is not the one legal next transaction phase"
        )
    expected_confirmation = f"mutate:{journal.current.transaction_id}:{phase}"
    if args.confirm != expected_confirmation:
        raise ReleaseCliError(
            f"mutation confirmation must be exactly {expected_confirmation}"
        )
    driver = _driver(args)
    rollback_reference = _rollback_reference(journal, args)
    journal.begin_mutation(target, rollback_reference=rollback_reference)
    # No validation, filesystem mutation, or object construction is permitted
    # between this account check and the injected phase dispatch.
    _discover_account(journal.current.account, journal.current.region)
    raw = _invoke_driver(driver, phase=phase, journal=journal)
    evidence = _phase_evidence(phase, raw)
    return journal.reconcile(persisted=True, evidence=evidence)


def _read_evidence(path: str | None) -> dict[str, str]:
    if path is None:
        return {}
    value = parse_canonical_object(read_regular_bytes(Path(path)))
    if any(not isinstance(item, str) for item in value.values()):
        raise ReleaseCliError("reconciliation evidence must contain strings")
    return dict(value)


def _preflight(args: argparse.Namespace) -> StagingTransactionV1:
    _, account, commit, tree = _preflight_identity(args)
    path = _journal_path(args)
    if path.exists() or path.is_symlink():
        journal = TransactionJournal.load(path)
        expected = (commit, tree, account, args.region)
        observed = (
            journal.current.source_commit,
            journal.current.source_tree,
            journal.current.account,
            journal.current.region,
        )
        if observed != expected:
            raise ReleaseCliError("existing journal belongs to another release")
    else:
        journal = TransactionJournal.create(
            path,
            source_commit=commit,
            source_tree=tree,
            account=account,
            region=args.region,
        )
    if journal.current.state == "NEW":
        return journal.advance_local("PREFLIGHTED")
    if journal.current.state == "PREFLIGHTED":
        return journal.current
    raise ReleaseCliError("preflight cannot rewind an advanced transaction")


def _resume(args: argparse.Namespace) -> StagingTransactionV1:
    journal = TransactionJournal.load(Path(args.resume))
    _check_requested_identity(journal.current, args)
    if args.reconcile:
        if journal.current.state != "UNCERTAIN":
            raise ReleaseCliError("only an UNCERTAIN journal can reconcile")
        persisted = args.reconcile == "persisted"
        if journal.current.uncertain_phase == "ROLLBACK":
            if args.evidence:
                raise ReleaseCliError("rollback reconciliation takes no evidence file")
            return journal.reconcile_rollback(persisted=persisted)
        evidence = _read_evidence(args.evidence) if persisted else {}
        return journal.reconcile(persisted=persisted, evidence=evidence)
    if journal.current.state == "UNCERTAIN":
        raise ReleaseCliError(
            "UNCERTAIN transaction requires explicit --reconcile persisted|absent"
        )
    target = journal.resume_target()
    if target is None:
        raise ReleaseCliError("transaction has no resumable phase")
    return _run_phase(journal, STATE_TO_PHASE[target], args)


def _rollback(args: argparse.Namespace) -> StagingTransactionV1:
    journal = TransactionJournal.load(_journal_path(args))
    _check_requested_identity(journal.current, args)
    transaction_id = args.rollback
    if transaction_id != journal.current.transaction_id:
        raise ReleaseCliError("rollback transaction ID differs from the journal")
    if journal.current.state != "VERIFIED":
        raise ReleaseCliError("rollback requires a VERIFIED transaction")
    expected_confirmation = f"rollback:{transaction_id}"
    if args.confirm != expected_confirmation:
        raise ReleaseCliError(
            f"rollback confirmation must be exactly {expected_confirmation}"
        )
    driver = _driver(args)
    reference = journal.current.rollback_reference
    journal.begin_rollback(reference)
    _discover_account(journal.current.account, journal.current.region)
    raw = _invoke_driver(driver, phase="rollback", journal=journal)
    if raw != {"rollback_reference": reference}:
        raise ReleaseCliError("rollback driver evidence differs from the journal")
    return journal.reconcile_rollback(persisted=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="credential-free local identity and journal preflight",
    )
    mode.add_argument(
        "--phase",
        choices=tuple(PHASE_TO_STATE),
        help="run exactly one confirmed staging phase",
    )
    mode.add_argument("--resume", metavar="JOURNAL", help="resume the legal next phase")
    mode.add_argument("--status", metavar="JOURNAL", help="print canonical journal state")
    mode.add_argument(
        "--rollback",
        metavar="VERIFIED_TRANSACTION_ID",
        help="roll back one exact verified transaction",
    )
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--account", default="")
    parser.add_argument("--region", default=REQUIRED_REGION)
    parser.add_argument("--commit", default="")
    parser.add_argument("--tree", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--rollback-reference", default="")
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--reconcile", choices=("persisted", "absent"))
    parser.add_argument("--evidence")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.status:
            if args.reconcile or args.evidence:
                raise ReleaseCliError("status accepts no reconciliation options")
            current = TransactionJournal.load(Path(args.status)).current
        elif args.preflight:
            current = _preflight(args)
        elif args.phase:
            journal = TransactionJournal.load(_journal_path(args))
            _check_requested_identity(journal.current, args)
            current = _run_phase(journal, args.phase, args)
        elif args.resume:
            current = _resume(args)
        elif args.rollback:
            current = _rollback(args)
        else:  # pragma: no cover - argparse enforces one mode
            raise ReleaseCliError("one release mode is required")
    except (
        ContractError,
        OSError,
        ReleaseCliError,
        subprocess.SubprocessError,
        TransactionError,
    ) as error:
        print(f"staging release: {error}", file=sys.stderr)
        return 1
    _emit(current)
    return 0


__all__ = ["PHASE_TO_STATE", "ReleaseCliError", "main"]
