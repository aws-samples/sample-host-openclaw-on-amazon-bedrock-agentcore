"""Credential-lazy command surface for the immutable staging transaction."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence

from release_tools.contracts import (
    ContractError,
    StagingTransactionV1,
    parse_canonical_object,
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


@contextmanager
def _driver(args: argparse.Namespace) -> Iterator[tuple[Path, str]]:
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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise ReleaseCliError("release phase driver is not a regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseCliError("release phase driver is not a regular file")
        if metadata.st_mode & 0o111 == 0:
            raise ReleaseCliError("release phase driver is not executable")
        chunks: list[bytes] = []
        remaining = 16 * 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not payload or len(payload) > 16 * 1024 * 1024:
        raise ReleaseCliError("release phase driver bytes are invalid")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="personal-operator-release-") as root:
        retained = Path(root) / "reviewed-operation"
        descriptor = os.open(
            retained,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        yield retained, digest


def _sanitized_environment(region: str) -> dict[str, str]:
    _validate_region(region)
    return {
        **os.environ,
        "CDK_DEFAULT_REGION": region,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
    }


def _discover_account(
    expected_account: str,
    region: str,
    *,
    environment: Mapping[str, str],
) -> None:
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
            env=dict(environment),
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
    mode: str,
    phase: str,
    journal: TransactionJournal,
    operation_sha256: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    current = journal.current
    completed = subprocess.run(
        [
            str(driver),
            "--mode",
            mode,
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
            "--operation-sha256",
            operation_sha256,
        ],
        check=False,
        capture_output=True,
        env=dict(environment),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseCliError(
            f"{phase} {mode} did not return authoritative evidence"
            + (f": {detail}" if detail else "")
        )
    try:
        evidence = parse_canonical_object(completed.stdout)
    except ContractError as error:
        raise ReleaseCliError(
            f"{phase} {mode} returned noncanonical evidence"
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


def _mutation_acknowledgement(phase: str, raw: Mapping[str, Any]) -> None:
    if raw != {"dispatched": True}:
        raise ReleaseCliError(f"{phase} mutation acknowledgement is not exact")


def _observation(
    phase: str,
    raw: Mapping[str, Any],
    *,
    journal: TransactionJournal,
    operation_sha256: str,
) -> tuple[bool, dict[str, str]]:
    expected_fields = {
        "account",
        "evidence",
        "operationSha256",
        "outcome",
        "phase",
        "region",
        "schema",
        "sourceCommit",
        "sourceTree",
        "transactionId",
    }
    if set(raw) != expected_fields:
        raise ReleaseCliError(f"{phase} observation has the wrong fields")
    expected_identity = {
        "account": journal.current.account,
        "operationSha256": operation_sha256,
        "phase": phase,
        "region": journal.current.region,
        "schema": "personal-operator.phase-observation.v1",
        "sourceCommit": journal.current.source_commit,
        "sourceTree": journal.current.source_tree,
        "transactionId": journal.current.transaction_id,
    }
    if any(raw.get(name) != value for name, value in expected_identity.items()):
        raise ReleaseCliError(
            f"{phase} observation differs from the release identity"
        )
    outcome = raw.get("outcome")
    evidence = raw.get("evidence")
    if outcome not in {"PERSISTED", "ABSENT"} or not isinstance(evidence, Mapping):
        raise ReleaseCliError(f"{phase} observation outcome is invalid")
    if outcome == "ABSENT":
        if evidence:
            raise ReleaseCliError(
                f"{phase} absent observation must contain no evidence"
            )
        return False, {}
    if phase == "rollback":
        expected = {"rollback_reference": journal.current.rollback_reference}
        if dict(evidence) != expected:
            raise ReleaseCliError(
                "rollback observation differs from the journal"
            )
        return True, {}
    return True, _phase_evidence(phase, evidence)


def _observe_and_reconcile(
    journal: TransactionJournal,
    *,
    phase: str,
    driver: Path,
    operation_sha256: str,
    environment: Mapping[str, str],
) -> StagingTransactionV1:
    _validate_region(journal.current.region)
    _discover_account(
        journal.current.account,
        journal.current.region,
        environment=environment,
    )
    raw = _invoke_driver(
        driver,
        mode="observe",
        phase=phase,
        journal=journal,
        operation_sha256=operation_sha256,
        environment=environment,
    )
    persisted, evidence = _observation(
        phase,
        raw,
        journal=journal,
        operation_sha256=operation_sha256,
    )
    if phase == "rollback":
        return journal.reconcile_rollback(
            persisted=persisted,
            operation_sha256=operation_sha256,
        )
    return journal.reconcile(
        persisted=persisted,
        operation_sha256=operation_sha256,
        evidence=evidence,
    )


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
    _validate_region(journal.current.region)
    target = PHASE_TO_STATE[phase]
    if journal.resume_target() != target:
        raise ReleaseCliError(
            f"{phase} is not the one legal next transaction phase"
        )
    with _driver(args) as (driver, operation_sha256):
        expected_confirmation = (
            f"mutate:{journal.current.transaction_id}:{phase}:"
            f"{operation_sha256}"
        )
        if args.confirm != expected_confirmation:
            raise ReleaseCliError(
                f"mutation confirmation must be exactly {expected_confirmation}"
            )
        rollback_reference = _rollback_reference(journal, args)
        journal.begin_mutation(
            target,
            rollback_reference=rollback_reference,
            operation_sha256=operation_sha256,
        )
        environment = _sanitized_environment(journal.current.region)
        _discover_account(
            journal.current.account,
            journal.current.region,
            environment=environment,
        )
        raw = _invoke_driver(
            driver,
            mode="mutate",
            phase=phase,
            journal=journal,
            operation_sha256=operation_sha256,
            environment=environment,
        )
        _mutation_acknowledgement(phase, raw)
        return _observe_and_reconcile(
            journal,
            phase=phase,
            driver=driver,
            operation_sha256=operation_sha256,
            environment=environment,
        )


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
    _validate_region(journal.current.region)
    if args.reconcile:
        if journal.current.state != "UNCERTAIN":
            raise ReleaseCliError("only an UNCERTAIN journal can reconcile")
        phase = (
            "rollback"
            if journal.current.uncertain_phase == "ROLLBACK"
            else STATE_TO_PHASE[journal.current.uncertain_phase]
        )
        with _driver(args) as (driver, operation_sha256):
            if operation_sha256 != journal.current.uncertain_operation_sha256:
                raise ReleaseCliError(
                    "reconciliation driver digest differs from the journal"
                )
            expected_confirmation = (
                f"reconcile:{journal.current.transaction_id}:{phase}:"
                f"{operation_sha256}"
            )
            if args.confirm != expected_confirmation:
                raise ReleaseCliError(
                    "reconciliation confirmation must be exactly "
                    f"{expected_confirmation}"
                )
            environment = _sanitized_environment(journal.current.region)
            return _observe_and_reconcile(
                journal,
                phase=phase,
                driver=driver,
                operation_sha256=operation_sha256,
                environment=environment,
            )
    if journal.current.state == "UNCERTAIN":
        raise ReleaseCliError(
            "UNCERTAIN transaction requires authoritative --reconcile"
        )
    target = journal.resume_target()
    if target is None:
        raise ReleaseCliError("transaction has no resumable phase")
    return _run_phase(journal, STATE_TO_PHASE[target], args)


def _rollback(args: argparse.Namespace) -> StagingTransactionV1:
    journal = TransactionJournal.load(_journal_path(args))
    _check_requested_identity(journal.current, args)
    _validate_region(journal.current.region)
    transaction_id = args.rollback
    if transaction_id != journal.current.transaction_id:
        raise ReleaseCliError("rollback transaction ID differs from the journal")
    if journal.current.state != "VERIFIED":
        raise ReleaseCliError("rollback requires a VERIFIED transaction")
    with _driver(args) as (driver, operation_sha256):
        expected_confirmation = (
            f"rollback:{transaction_id}:{operation_sha256}"
        )
        if args.confirm != expected_confirmation:
            raise ReleaseCliError(
                f"rollback confirmation must be exactly {expected_confirmation}"
            )
        reference = journal.current.rollback_reference
        journal.begin_rollback(
            reference,
            operation_sha256=operation_sha256,
        )
        environment = _sanitized_environment(journal.current.region)
        _discover_account(
            journal.current.account,
            journal.current.region,
            environment=environment,
        )
        raw = _invoke_driver(
            driver,
            mode="mutate",
            phase="rollback",
            journal=journal,
            operation_sha256=operation_sha256,
            environment=environment,
        )
        _mutation_acknowledgement("rollback", raw)
        return _observe_and_reconcile(
            journal,
            phase="rollback",
            driver=driver,
            operation_sha256=operation_sha256,
            environment=environment,
        )


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
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="authoritatively observe and reconcile one UNCERTAIN phase",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.status:
            if args.reconcile:
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
