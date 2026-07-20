"""Credential-lazy command surface for the immutable staging transaction."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from release_tools.contracts import (
    ContractError,
    ProductionObservationConfigV1,
    StagingTransactionV1,
    parse_canonical_object,
    read_regular_bytes,
)
from release_tools.evidence_runtime import (
    EvidenceRuntimeError,
    snapshot_evidence_runtime,
)
from release_tools.production_observation import (
    HttpsArtifactBlobReader,
    ProductionObservationError,
    compose_production_evidence,
)
from release_tools.transaction import TransactionError, TransactionJournal


REQUIRED_REGION = "eu-west-1"
EXECUTING_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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
_ACCOUNT = re.compile(r"[0-9]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_AWS_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+=,-]{0,127}")
_ACCOUNT_OWNER_UIDS = frozenset({0, os.getuid()})
_LOGIN_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
TRUSTED_AWS_CLI_CANDIDATES = (
    Path("/usr/local/bin/aws"),
    Path("/opt/homebrew/bin/aws"),
    Path("/usr/bin/aws"),
    _LOGIN_HOME / ".local" / "bin" / "aws",
)
TRUSTED_GIT_CANDIDATES = (
    Path("/usr/bin/git"),
    Path("/usr/local/bin/git"),
    Path("/opt/homebrew/bin/git"),
)


class ReleaseCliError(RuntimeError):
    """The requested release operation is unsafe or incomplete."""


class EvidenceComposer(Protocol):
    def observe_phase(
        self,
        phase: str,
        transaction: StagingTransactionV1,
    ) -> tuple[bool, Mapping[str, str]]: ...


EvidenceComposerFactory = Callable[
    [ProductionObservationConfigV1], EvidenceComposer
]


def _reviewed_operation_sha256(
    driver: bytes,
    observation_config: ProductionObservationConfigV1,
) -> str:
    """Bind the executable and reviewed live-observation inputs as one operation."""

    if not isinstance(driver, bytes) or not driver:
        raise ReleaseCliError("release operation bytes are invalid")
    config = observation_config.to_bytes()
    framed = b"".join(
        (
            b"personal-operator.release-operation.v2\0",
            len(driver).to_bytes(8, "big"),
            driver,
            len(config).to_bytes(8, "big"),
            config,
        )
    )
    return "sha256:" + hashlib.sha256(framed).hexdigest()


def _trusted_executable(
    candidates: Sequence[Path],
    *,
    description: str,
) -> Path:
    for candidate in candidates:
        if not isinstance(candidate, Path) or not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in _ACCOUNT_OWNER_UIDS
            and metadata.st_mode & 0o111
            and metadata.st_mode & 0o022 == 0
        ):
            return resolved
    raise ReleaseCliError(
        f"{description} requires a trusted absolute executable path"
    )


def _trusted_git() -> Path:
    return _trusted_executable(
        TRUSTED_GIT_CANDIDATES,
        description="Git identity verification",
    )


def _git(root: Path, *arguments: str) -> str:
    executable = _trusted_git()
    try:
        return subprocess.check_output(
            [
                str(executable),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *arguments,
            ],
            text=True,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
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


def _assert_release_checkout(
    journal: TransactionJournal,
    root_value: Path,
) -> Path:
    try:
        root = Path(root_value).resolve(strict=True)
    except OSError as error:
        raise ReleaseCliError("release checkout root does not exist") from error
    if not root.is_dir():
        raise ReleaseCliError("release checkout root is not a directory")
    current = journal.current
    if (
        _git(root, "rev-parse", "HEAD") != current.source_commit
        or _git(root, "rev-parse", "HEAD^{tree}") != current.source_tree
        or _git(root, "status", "--porcelain", "--untracked-files=all")
    ):
        raise ReleaseCliError(
            "release checkout no longer matches the clean journaled commit and tree"
        )
    return root


def _assert_executing_repository(root_value: Path) -> None:
    try:
        requested = Path(root_value).resolve(strict=True)
    except OSError as error:
        raise ReleaseCliError("release root does not exist") from error
    if requested != EXECUTING_REPOSITORY_ROOT:
        raise ReleaseCliError(
            "release root differs from the executing repository"
        )


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


def _load_observation_config(
    args: argparse.Namespace,
    journal: TransactionJournal,
) -> ProductionObservationConfigV1:
    configured = getattr(args, "observation_config", None)
    path = (
        Path(configured)
        if configured is not None
        else Path(str(journal.path) + ".production-observation.json")
    )
    try:
        config = ProductionObservationConfigV1.from_bytes(read_regular_bytes(path))
    except (ContractError, OSError) as error:
        raise ReleaseCliError(
            "exact production observation config is required before mutation"
        ) from error
    current = journal.current
    if (
        config.source_commit,
        config.source_tree,
        config.account,
        config.region,
    ) != (
        current.source_commit,
        current.source_tree,
        current.account,
        current.region,
    ):
        raise ReleaseCliError(
            "production observation config differs from the release journal"
        )
    return config


def _production_composer(
    config: ProductionObservationConfigV1,
    *,
    ca_descriptor: int | None = None,
) -> EvidenceComposer:
    """Freeze exact credentials into regional clients after account discovery."""

    try:
        import boto3
        from botocore.config import Config
        from botocore import httpsession

        if ca_descriptor is not None:
            ca_path = _descriptor_path(ca_descriptor)

            def retained_ca_path() -> str:
                try:
                    os.lseek(ca_descriptor, 0, os.SEEK_SET)
                except OSError as error:
                    raise ReleaseCliError(
                        "authenticated AWS CA bundle is unavailable"
                    ) from error
                return ca_path

            httpsession.where = retained_ca_path

        client_config = Config(
            region_name=config.region,
            ignore_configured_endpoint_urls=True,
            proxies={},
            retries={"mode": "standard", "max_attempts": 3},
        )
        source_session = boto3.Session(region_name=config.region)
        credentials = source_session.get_credentials()
        if credentials is None:
            raise ReleaseCliError(
                "production observation credentials are unavailable"
            )
        frozen = credentials.get_frozen_credentials()
        if (
            not isinstance(frozen.access_key, str)
            or not frozen.access_key
            or not isinstance(frozen.secret_key, str)
            or not frozen.secret_key
            or (
                frozen.token is not None
                and (not isinstance(frozen.token, str) or not frozen.token)
            )
        ):
            raise ReleaseCliError(
                "production observation credentials cannot be frozen"
            )
        frozen_arguments = {
            "aws_access_key_id": frozen.access_key,
            "aws_secret_access_key": frozen.secret_key,
        }
        if frozen.token is not None:
            frozen_arguments["aws_session_token"] = frozen.token
        session = boto3.Session(
            region_name=config.region,
            **frozen_arguments,
        )
        sts = session.client(
            "sts",
            region_name=config.region,
            config=client_config,
        )
        identity = sts.get_caller_identity()
        if (
            not isinstance(identity, Mapping)
            or identity.get("Account") != config.account
        ):
            raise ReleaseCliError(
                "production observation credentials differ from the release account"
            )
        artifact_ca_file = (
            retained_ca_path() if ca_descriptor is not None else None
        )
        return compose_production_evidence(
            ecr_client=session.client(
                "ecr",
                region_name=config.region,
                config=client_config,
            ),
            artifact_blob_reader=HttpsArtifactBlobReader(
                ca_file=artifact_ca_file,
            ),
            agentcore_client=session.client(
                "bedrock-agentcore-control",
                region_name=config.region,
                config=client_config,
            ),
            cloudformation_client=session.client(
                "cloudformation",
                region_name=config.region,
                config=client_config,
            ),
            config=config,
        )
    except ReleaseCliError:
        raise
    except Exception as error:
        raise ReleaseCliError(
            "production observation authority is unavailable"
        ) from error


_EVIDENCE_MODULE_ROOTS = frozenset(
    {
        "_awscrt",
        "awscrt",
        "boto3",
        "botocore",
        "certifi",
        "dateutil",
        "jmespath",
        "s3transfer",
        "six",
        "urllib3",
    }
)


def _descriptor_path(descriptor: int) -> str:
    if not isinstance(descriptor, int) or descriptor < 0:
        raise ReleaseCliError("authenticated AWS CA descriptor is invalid")
    for root in (Path("/dev/fd"), Path("/proc/self/fd")):
        candidate = root / str(descriptor)
        if candidate.exists():
            return str(candidate)
    raise ReleaseCliError("authenticated AWS CA descriptor path is unavailable")


class _DescriptorBoundComposer:
    """Keep one unlinked, read-only CA inode alive for one live observation."""

    def __init__(self, delegate: EvidenceComposer, ca_descriptor: int) -> None:
        self._delegate = delegate
        self._ca_descriptor = ca_descriptor

    def _close(self) -> None:
        descriptor = self._ca_descriptor
        if descriptor >= 0:
            self._ca_descriptor = -1
            try:
                os.close(descriptor)
            except OSError:
                pass

    def observe_phase(
        self,
        phase: str,
        transaction: StagingTransactionV1,
    ) -> tuple[bool, Mapping[str, str]]:
        if self._ca_descriptor < 0:
            raise ReleaseCliError(
                "production observation authority was already consumed"
            )
        try:
            return self._delegate.observe_phase(phase, transaction)
        finally:
            self._close()

    def __del__(self) -> None:
        self._close()


def _authenticated_production_composer(
    config: ProductionObservationConfigV1,
    *,
    site_packages: Path,
) -> EvidenceComposer:
    """Load clients from one reviewed SDK snapshot, then erase its file tree."""

    preloaded = sorted(
        name
        for name in sys.modules
        if name.partition(".")[0] in _EVIDENCE_MODULE_ROOTS
    )
    if preloaded:
        raise ReleaseCliError(
            "production evidence SDK modules were loaded before authentication"
        )
    try:
        with tempfile.TemporaryDirectory(
            prefix="personal-operator-evidence-runtime-"
        ) as temporary_root:
            retained = Path(temporary_root) / "sdk"
            observed = snapshot_evidence_runtime(
                site_packages,
                destination=retained,
            )
            if observed != config.evidence_runtime_sha256:
                raise ReleaseCliError(
                    "production evidence runtime differs from the reviewed digest"
                )
            ca_file = retained / "botocore" / "cacert.pem"
            ca_descriptor = -1
            try:
                ca_descriptor = os.open(
                    ca_file,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                os.set_inheritable(ca_descriptor, False)
            except OSError as error:
                if ca_descriptor >= 0:
                    os.close(ca_descriptor)
                raise ReleaseCliError(
                    "authenticated AWS CA bundle is unavailable"
                ) from error
            ca_metadata = os.fstat(ca_descriptor)
            if (
                os.get_inheritable(ca_descriptor)
                or not stat.S_ISREG(ca_metadata.st_mode)
                or ca_metadata.st_size <= 0
            ):
                os.close(ca_descriptor)
                raise ReleaseCliError(
                    "authenticated AWS CA bundle is invalid"
                )
            retained_text = str(retained)
            sys.path.insert(0, retained_text)
            previous_dont_write_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            importlib.invalidate_caches()
            try:
                composer = _production_composer(
                    config,
                    ca_descriptor=ca_descriptor,
                )
            except Exception:
                os.close(ca_descriptor)
                raise
            finally:
                sys.dont_write_bytecode = previous_dont_write_bytecode
                try:
                    sys.path.remove(retained_text)
                except ValueError:
                    pass
                importlib.invalidate_caches()
            return _DescriptorBoundComposer(composer, ca_descriptor)
    except EvidenceRuntimeError as error:
        raise ReleaseCliError(
            "production evidence runtime cannot be authenticated"
        ) from error


@contextmanager
def _driver(args: argparse.Namespace) -> Iterator[tuple[bytes, str]]:
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
    yield payload, digest


_FORWARDED_AWS_CREDENTIALS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
)


def _sanitized_environment(account: str, region: str) -> dict[str, str]:
    _validate_region(region)
    environment = {
        name: os.environ[name]
        for name in _FORWARDED_AWS_CREDENTIALS
        if os.environ.get(name)
    }
    profile = os.environ.get("AWS_PROFILE", "")
    default_profile = os.environ.get("AWS_DEFAULT_PROFILE", "")
    if profile and default_profile and profile != default_profile:
        raise ReleaseCliError("AWS profile selectors conflict")
    profile = profile or default_profile
    if profile and _AWS_PROFILE.fullmatch(profile) is None:
        raise ReleaseCliError("AWS profile selector is invalid")
    aws_home = _LOGIN_HOME / ".aws"
    environment.update({
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AWS_CONFIG_FILE": str(aws_home / "config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(aws_home / "credentials"),
        "AWS_LOGIN_CACHE_DIRECTORY": str(aws_home / "login" / "cache"),
        "AWS_SDK_LOAD_CONFIG": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CDK_DEFAULT_ACCOUNT": account,
        "CDK_DEFAULT_REGION": region,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
    })
    if profile:
        environment["AWS_PROFILE"] = profile
    return environment


@contextmanager
def _scoped_release_environment(
    environment: Mapping[str, str],
) -> Iterator[None]:
    """Expose exactly the account-checked environment to live SDK adapters."""

    exact = dict(environment)
    if any(
        not isinstance(name, str)
        or not name
        or not isinstance(value, str)
        or "\x00" in name
        or "\x00" in value
        for name, value in exact.items()
    ):
        raise ReleaseCliError("release environment is malformed")
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(exact)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _discover_account(
    expected_account: str,
    region: str,
    *,
    environment: Mapping[str, str],
) -> None:
    """Touch credentials only after durable intent and immediately pre-dispatch."""

    executable = _trusted_aws_cli()
    try:
        completed = subprocess.run(
            [
                str(executable),
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


def _trusted_aws_cli() -> Path:
    """Resolve AWS CLI only from fixed absolute, owner-controlled locations."""
    return _trusted_executable(
        TRUSTED_AWS_CLI_CANDIDATES,
        description="AWS credential discovery",
    )


def _invoke_mutation_driver(
    driver: bytes,
    *,
    phase: str,
    journal: TransactionJournal,
    operation_sha256: str,
    driver_sha256: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    current = journal.current
    expected_digest = "sha256:" + hashlib.sha256(driver).hexdigest()
    if expected_digest != driver_sha256:
        raise ReleaseCliError("release operation bytes differ from the journal digest")
    with tempfile.TemporaryDirectory(
        prefix="personal-operator-operation-"
    ) as temporary_root:
        root = Path(temporary_root)
        retained = root / "reviewed-operation"
        descriptor = os.open(
            retained,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(driver)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if retained.read_bytes() != driver:
            raise ReleaseCliError("release operation bytes changed before invocation")
        child_environment = dict(environment)
        child_environment["HOME"] = str(root)
        child_environment["TMPDIR"] = str(root)
        completed = subprocess.run(
            [
                str(retained),
                "--mode",
                "mutate",
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
            cwd=root,
            env=child_environment,
        )
        try:
            after = retained.read_bytes()
        except OSError as error:
            raise ReleaseCliError(
                "release operation bytes changed during invocation"
            ) from error
        if after != driver:
            raise ReleaseCliError(
                "release operation bytes changed during invocation"
            )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseCliError(
            f"{phase} mutation driver did not acknowledge dispatch"
            + (f": {detail}" if detail else "")
        )
    try:
        evidence = parse_canonical_object(completed.stdout)
    except ContractError as error:
        raise ReleaseCliError(
            f"{phase} mutation driver returned noncanonical acknowledgement"
        ) from error
    return evidence


def _mutation_acknowledgement(phase: str, raw: Mapping[str, Any]) -> None:
    if raw != {"dispatched": True}:
        raise ReleaseCliError(f"{phase} mutation acknowledgement is not exact")


def _prepare_composer(
    journal: TransactionJournal,
    *,
    environment: Mapping[str, str],
    observation_config: ProductionObservationConfigV1,
    composer_factory: EvidenceComposerFactory,
) -> EvidenceComposer:
    """Authenticate and retain the live authority before untrusted dispatch."""

    _discover_account(
        journal.current.account,
        journal.current.region,
        environment=environment,
    )
    with _scoped_release_environment(environment):
        return composer_factory(observation_config)


def _observe_and_reconcile(
    journal: TransactionJournal,
    *,
    phase: str,
    operation_sha256: str,
    environment: Mapping[str, str],
    composer: EvidenceComposer,
    release_root: Path | None = None,
) -> StagingTransactionV1:
    _validate_region(journal.current.region)
    if release_root is not None:
        _assert_release_checkout(journal, release_root)
    _discover_account(
        journal.current.account,
        journal.current.region,
        environment=environment,
    )
    with _scoped_release_environment(environment):
        persisted, raw_evidence = composer.observe_phase(phase, journal.current)
    if not isinstance(persisted, bool) or not isinstance(raw_evidence, Mapping):
        raise ReleaseCliError(
            f"{phase} live observation authority returned an invalid result"
        )
    evidence = dict(raw_evidence)
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
    *,
    observation_config: ProductionObservationConfigV1,
    composer_factory: EvidenceComposerFactory,
) -> StagingTransactionV1:
    _validate_region(journal.current.region)
    release_root_value = getattr(args, "root", None)
    release_root = (
        _assert_release_checkout(journal, Path(release_root_value))
        if release_root_value is not None
        else None
    )
    target = PHASE_TO_STATE[phase]
    if journal.resume_target() != target:
        raise ReleaseCliError(
            f"{phase} is not the one legal next transaction phase"
        )
    with _driver(args) as (driver, driver_sha256):
        operation_sha256 = _reviewed_operation_sha256(
            driver,
            observation_config,
        )
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
        environment = _sanitized_environment(
            journal.current.account,
            journal.current.region,
        )
        composer = _prepare_composer(
            journal,
            environment=environment,
            observation_config=observation_config,
            composer_factory=composer_factory,
        )
        raw = _invoke_mutation_driver(
            driver,
            phase=phase,
            journal=journal,
            operation_sha256=operation_sha256,
            driver_sha256=driver_sha256,
            environment=environment,
        )
        _mutation_acknowledgement(phase, raw)
        return _observe_and_reconcile(
            journal,
            phase=phase,
            operation_sha256=operation_sha256,
            environment=environment,
            composer=composer,
            release_root=release_root,
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
    release_root = _assert_release_checkout(journal, Path(args.root))
    if args.reconcile:
        if journal.current.state != "UNCERTAIN":
            raise ReleaseCliError("only an UNCERTAIN journal can reconcile")
        phase = (
            "rollback"
            if journal.current.uncertain_phase == "ROLLBACK"
            else STATE_TO_PHASE[journal.current.uncertain_phase]
        )
        observation_config = _load_observation_config(args, journal)
        with _driver(args) as (driver, _driver_sha256):
            operation_sha256 = _reviewed_operation_sha256(
                driver,
                observation_config,
            )
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
            environment = _sanitized_environment(
                journal.current.account,
                journal.current.region,
            )
            composer = _prepare_composer(
                journal,
                environment=environment,
                observation_config=observation_config,
                composer_factory=args.composer_factory,
            )
            return _observe_and_reconcile(
                journal,
                phase=phase,
                operation_sha256=operation_sha256,
                environment=environment,
                composer=composer,
                release_root=release_root,
            )
    if journal.current.state == "UNCERTAIN":
        raise ReleaseCliError(
            "UNCERTAIN transaction requires authoritative --reconcile"
        )
    target = journal.resume_target()
    if target is None:
        raise ReleaseCliError("transaction has no resumable phase")
    return _run_phase(
        journal,
        STATE_TO_PHASE[target],
        args,
        observation_config=_load_observation_config(args, journal),
        composer_factory=args.composer_factory,
    )


def _rollback(args: argparse.Namespace) -> StagingTransactionV1:
    journal = TransactionJournal.load(_journal_path(args))
    _check_requested_identity(journal.current, args)
    _validate_region(journal.current.region)
    release_root = _assert_release_checkout(journal, Path(args.root))
    transaction_id = args.rollback
    if transaction_id != journal.current.transaction_id:
        raise ReleaseCliError("rollback transaction ID differs from the journal")
    if journal.current.state != "VERIFIED":
        raise ReleaseCliError("rollback requires a VERIFIED transaction")
    observation_config = _load_observation_config(args, journal)
    with _driver(args) as (driver, driver_sha256):
        operation_sha256 = _reviewed_operation_sha256(
            driver,
            observation_config,
        )
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
        environment = _sanitized_environment(
            journal.current.account,
            journal.current.region,
        )
        composer = _prepare_composer(
            journal,
            environment=environment,
            observation_config=observation_config,
            composer_factory=args.composer_factory,
        )
        raw = _invoke_mutation_driver(
            driver,
            phase="rollback",
            journal=journal,
            operation_sha256=operation_sha256,
            driver_sha256=driver_sha256,
            environment=environment,
        )
        _mutation_acknowledgement("rollback", raw)
        return _observe_and_reconcile(
            journal,
            phase="rollback",
            operation_sha256=operation_sha256,
            environment=environment,
            composer=composer,
            release_root=release_root,
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
        "--observation-config",
        type=Path,
        help=(
            "reviewed canonical live-observation config; defaults beside the journal"
        ),
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="authoritatively observe and reconcile one UNCERTAIN phase",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    composer_factory: EvidenceComposerFactory | None = None,
    production_site_packages: Path | None = None,
) -> int:
    args = _parser().parse_args(argv)
    production_entrypoint = composer_factory is None
    if composer_factory is None:
        def production_factory(
            config: ProductionObservationConfigV1,
        ) -> EvidenceComposer:
            if production_site_packages is None:
                raise ReleaseCliError(
                    "production evidence runtime path is unavailable"
                )
            return _authenticated_production_composer(
                config,
                site_packages=production_site_packages,
            )

        args.composer_factory = production_factory
    else:
        args.composer_factory = composer_factory
    try:
        if production_entrypoint and (
            args.phase
            or args.resume
            or args.rollback
            or args.reconcile
            or args.driver is not None
        ):
            raise ReleaseCliError(
                "v1 mutation is disabled; the legacy driver/composer completion "
                "route is retired at the operator entrypoint; use only "
                "credential-free preflight/status until the reviewed v2 session "
                "transaction is available"
            )
        if production_entrypoint and not args.status:
            _assert_executing_repository(args.root)
        if args.status:
            if args.reconcile:
                raise ReleaseCliError("status accepts no reconciliation options")
            current = TransactionJournal.load(Path(args.status)).current
        elif args.preflight:
            current = _preflight(args)
        elif args.phase:
            journal = TransactionJournal.load(_journal_path(args))
            _check_requested_identity(journal.current, args)
            _assert_release_checkout(journal, Path(args.root))
            current = _run_phase(
                journal,
                args.phase,
                args,
                observation_config=_load_observation_config(args, journal),
                composer_factory=args.composer_factory,
            )
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
        ProductionObservationError,
        subprocess.SubprocessError,
        TransactionError,
    ) as error:
        print(f"staging release: {error}", file=sys.stderr)
        return 1
    _emit(current)
    return 0


__all__ = ["PHASE_TO_STATE", "ReleaseCliError", "main"]
