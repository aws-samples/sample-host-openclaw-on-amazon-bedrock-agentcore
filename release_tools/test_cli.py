from __future__ import annotations

import hashlib
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


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repo"
    repository.mkdir(parents=True)
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
        f"""#!{sys.executable}
import argparse
import hashlib
import json
import os
import pathlib
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--mode", required=True)
parser.add_argument("--phase", required=True)
parser.add_argument("--journal", required=True)
parser.add_argument("--transaction-id", required=True)
parser.add_argument("--source-commit", required=True)
parser.add_argument("--source-tree", required=True)
parser.add_argument("--account", required=True)
parser.add_argument("--region", required=True)
parser.add_argument("--operation-sha256", required=True)
args = parser.parse_args()
with pathlib.Path(os.environ["RELEASE_CALL_LOG"]).open("a", encoding="utf-8") as log:
    log.write(
        f"driver {{args.mode}} <{{args.phase}}> region="
        f"<{{os.environ.get('CDK_DEFAULT_REGION', '')}}>"
        f"/<{{os.environ.get('AWS_REGION', '')}}>"
        f"/<{{os.environ.get('AWS_DEFAULT_REGION', '')}}>\\n"
    )
if args.mode == "mutate":
    if os.environ.get("RELEASE_FAIL_PHASE") == args.phase:
        raise SystemExit(75)
    value = {{}} if os.environ.get("RELEASE_BAD_ACK_PHASE") == args.phase else {{"dispatched": True}}
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0)
if args.mode != "observe":
    raise SystemExit(76)
if os.environ.get("RELEASE_FAIL_OBSERVE_PHASE") == args.phase:
    raise SystemExit(77)
outcome = os.environ.get("RELEASE_OBSERVE_OUTCOME", "PERSISTED")
evidence = {{}}
digest = "sha256:" + "0" * 64
image_uri = (
    f"{{args.account}}.dkr.ecr.{{args.region}}.amazonaws.com/"
    f"personal-operator/bridge@{{digest}}"
)
context = {{
    "account": args.account,
    "region": args.region,
    "runtimeArn": (
        f"arn:aws:bedrock-agentcore:{{args.region}}:{{args.account}}:"
        "agent/12345678-1234-1234-1234-123456789abc:7"
    ),
    "runtimeEndpointId": "ReleaseEndpoint-ABCDEFGHIJ",
    "runtimeEndpointName": f"release_{{args.source_commit}}",
    "runtimeId": "Runtime-ABCDEFGHIJ",
    "runtimeImageUri": image_uri,
    "runtimeVersion": "7",
    "schema": "personal-operator.runtime-context.v3",
    "sourceCommit": args.source_commit,
}}
if outcome == "PERSISTED":
    if args.phase == "image":
        if os.environ.get("RELEASE_LEGACY_IMAGE") == "1":
            evidence = {{"runtime_image_digest": digest}}
        else:
            evidence = {{"runtime_image_evidence": {{
                "account": args.account,
                "commitTag": f"commit-{{args.source_commit}}",
                "criticalFindings": 0,
                "highFindings": 0,
                "imageDigest": digest,
                "imageSizeBytes": 1,
                "imageUri": image_uri,
                "provenanceSha256": "2" * 64,
                "region": args.region,
                "repositoryName": "personal-operator/bridge",
                "sbomSha256": "1" * 64,
                "scanStatus": "COMPLETE",
                "schema": "personal-operator.runtime-image-evidence.v1",
                "signatureStatus": "SIGNED",
                "signingProfileArn": (
                    f"arn:aws:signer:{{args.region}}:{{args.account}}:/"
                    "signing-profiles/personal_operator_bridge"
                ),
                "sourceCommit": args.source_commit,
                "sourceTree": args.source_tree,
            }}}}
    elif args.phase == "runtime":
        evidence = {{"runtime_id": "Runtime-ABCDEFGHIJ", "runtime_version": "7"}}
    elif args.phase in {{"endpoint", "context"}}:
        if args.phase == "endpoint" and os.environ.get("RELEASE_EMPTY_ENDPOINT") == "1":
            evidence = {{}}
        else:
            evidence = {{"runtime_context": context}}
            if args.phase == "context":
                payload = (json.dumps(context, separators=(",", ":"), sort_keys=True) + "\\n").encode()
                evidence["runtime_context_sha256"] = hashlib.sha256(payload).hexdigest()
    elif args.phase == "rollback":
        evidence = {{"rollback_reference": os.environ["RELEASE_ROLLBACK_REFERENCE"]}}
observation = {{
    "account": args.account,
    "evidence": evidence,
    "operationSha256": args.operation_sha256,
    "outcome": outcome,
    "phase": args.phase,
    "region": args.region,
    "schema": "personal-operator.phase-observation.v1",
    "sourceCommit": args.source_commit,
    "sourceTree": args.source_tree,
    "transactionId": args.transaction_id,
}}
print(json.dumps(observation, separators=(",", ":"), sort_keys=True))
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
    operation_sha256 = _sha256(fixture["driver"])
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
        args.extend(
            [
                "--confirm",
                f"mutate:{transaction_id}:{phase}:{operation_sha256}",
            ]
        )
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


def test_mutation_rejects_a_symlinked_driver_before_write_ahead_or_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    link = tmp_path / "phase-driver-link"
    link.symlink_to(fixture["driver"])

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--phase",
        "foundation",
        "--driver",
        str(link),
        "--rollback-reference",
        str(fixture["rollback"]),
        "--confirm",
        (
            f"mutate:release_{fixture['commit']}:foundation:"
            f"{_sha256(fixture['driver'])}"
        ),
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.casefold()
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
        "driver mutate <foundation> region=<eu-west-1>/<eu-west-1>/<eu-west-1>",
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
        "driver observe <foundation> region=<eu-west-1>/<eu-west-1>/<eu-west-1>",
    ]
    assert TransactionJournal.load(fixture["journal"]).current.state == (
        "FOUNDATION_READY"
    )


def test_phase_revalidates_region_before_write_ahead_or_credentials(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0

    completed = _phase(fixture, "foundation", AWS_REGION="us-east-1")

    assert completed.returncode != 0
    assert "AWS_REGION" in completed.stderr
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "PREFLIGHTED"


def test_mutation_requires_typed_ack_and_authoritative_observation(
    tmp_path: Path,
) -> None:
    bad_ack = _fixture(tmp_path / "ack")
    assert _preflight(bad_ack).returncode == 0

    rejected = _phase(
        bad_ack,
        "foundation",
        RELEASE_BAD_ACK_PHASE="foundation",
    )

    assert rejected.returncode != 0
    assert "acknowledgement" in rejected.stderr
    assert TransactionJournal.load(bad_ack["journal"]).current.state == "UNCERTAIN"

    no_observation = _fixture(tmp_path / "observe")
    assert _preflight(no_observation).returncode == 0
    ambiguous = _phase(
        no_observation,
        "foundation",
        RELEASE_FAIL_OBSERVE_PHASE="foundation",
    )

    assert ambiguous.returncode != 0
    assert "observe" in ambiguous.stderr
    assert TransactionJournal.load(no_observation["journal"]).current.state == (
        "UNCERTAIN"
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
    )

    assert failed.returncode != 0
    assert after_failure.state == "UNCERTAIN"
    assert after_failure.last_stable_state == "FOUNDATION_READY"
    assert after_failure.uncertain_phase == "IMAGE_PUBLISHED"
    assert later.returncode != 0
    assert resume.returncode != 0
    assert "reconcile" in resume.stderr.casefold()
    assert fixture["log"].read_text(encoding="utf-8").splitlines().count(
        "driver mutate <image> region=<eu-west-1>/<eu-west-1>/<eu-west-1>"
    ) == 1
    assert "<runtime>" not in fixture["log"].read_text(encoding="utf-8")


def test_image_cannot_stabilize_on_a_bare_driver_asserted_digest(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0

    completed = _phase(fixture, "image", RELEASE_LEGACY_IMAGE="1")

    assert completed.returncode != 0
    assert "RuntimeImageEvidence" in completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.last_stable_state == "FOUNDATION_READY"


def test_endpoint_cannot_stabilize_without_a_typed_live_runtime_context(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image").returncode == 0
    assert _phase(fixture, "runtime").returncode == 0

    completed = _phase(fixture, "endpoint", RELEASE_EMPTY_ENDPOINT="1")

    assert completed.returncode != 0
    assert "RuntimeContextV3" in completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "UNCERTAIN"
    assert current.last_stable_state == "RUNTIME_READY"


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
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{_sha256(fixture['driver'])}"
        ),
        RELEASE_OBSERVE_OUTCOME="ABSENT",
    )
    resumed = _phase(fixture, "image")

    assert reconciled.returncode == 0, reconciled.stderr
    assert resumed.returncode == 0, resumed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "IMAGE_PUBLISHED"
    assert current.runtime_image_digest == "sha256:" + "0" * 64


def test_reconciliation_rejects_operator_outcome_and_changed_driver(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0
    before_calls = fixture["log"].read_text(encoding="utf-8")

    operator_claim = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "persisted",
    )
    fixture["driver"].write_text(
        fixture["driver"].read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    fixture["driver"].chmod(0o755)
    changed_digest = _sha256(fixture["driver"])
    replaced = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{changed_digest}"
        ),
    )

    assert operator_claim.returncode != 0
    assert replaced.returncode != 0
    assert "digest differs" in replaced.stderr
    assert fixture["log"].read_text(encoding="utf-8") == before_calls
    assert TransactionJournal.load(fixture["journal"]).current.state == "UNCERTAIN"


def test_reconciliation_revalidates_region_before_live_observation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    assert _preflight(fixture).returncode == 0
    assert _phase(fixture, "foundation").returncode == 0
    assert _phase(fixture, "image", RELEASE_FAIL_PHASE="image").returncode != 0
    before_calls = fixture["log"].read_text(encoding="utf-8")

    completed = _run(
        fixture,
        "--resume",
        str(fixture["journal"]),
        "--reconcile",
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        (
            f"reconcile:release_{fixture['commit']}:image:"
            f"{_sha256(fixture['driver'])}"
        ),
        AWS_DEFAULT_REGION="us-east-1",
    )

    assert completed.returncode != 0
    assert "AWS_DEFAULT_REGION" in completed.stderr
    assert fixture["log"].read_text(encoding="utf-8") == before_calls
    assert TransactionJournal.load(fixture["journal"]).current.state == "UNCERTAIN"


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
            "uncertainOperationSha256": "",
        }
    )
    write_new_contract(fixture["journal"], verified)
    transaction_id = f"release_{fixture['commit']}"
    operation_sha256 = _sha256(fixture["driver"])

    poisoned = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--rollback",
        transaction_id,
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"rollback:{transaction_id}:{operation_sha256}",
        CDK_DEFAULT_REGION="us-east-1",
    )
    assert poisoned.returncode != 0
    assert "CDK_DEFAULT_REGION" in poisoned.stderr
    assert not fixture["log"].exists()
    assert TransactionJournal.load(fixture["journal"]).current.state == "VERIFIED"

    completed = _run(
        fixture,
        "--journal",
        str(fixture["journal"]),
        "--rollback",
        transaction_id,
        "--driver",
        str(fixture["driver"]),
        "--confirm",
        f"rollback:{transaction_id}:{operation_sha256}",
    )

    assert completed.returncode == 0, completed.stderr
    current = TransactionJournal.load(fixture["journal"]).current
    assert current.state == "ROLLED_BACK"
    assert current.runtime_endpoint_name == f"release_{fixture['commit']}"
    assert fixture["log"].read_text(encoding="utf-8").splitlines()[-2:] == [
        "aws <sts get-caller-identity --query Account --output text --region eu-west-1>",
        "driver observe <rollback> region=<eu-west-1>/<eu-west-1>/<eu-west-1>",
    ]


def test_status_rejects_noncanonical_journal_without_aws_access(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["journal"].write_bytes(canonical_json_bytes({"schema": "wrong"}))

    completed = _run(fixture, "--status", str(fixture["journal"]))

    assert completed.returncode != 0
    assert not fixture["log"].exists()
