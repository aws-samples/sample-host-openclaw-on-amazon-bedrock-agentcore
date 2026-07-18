"""Fail-closed selection of the authenticated trusted Lambda ZIP asset."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path

from release_tools.contracts import ContractError, TrustedLambdaAssetV2
from release_tools.lambda_asset import verify_trusted_lambda_artifact


SCHEMA = TrustedLambdaAssetV2.SCHEMA
SYNTHETIC_ACCOUNT = "000000000000"


class TrustedLambdaAssetError(RuntimeError):
    """The dependency-bearing Lambda ZIP is absent or not authenticated."""


@dataclass(frozen=True, slots=True)
class ResolvedTrustedLambdaAsset:
    path: str
    asset_hash: str | None
    synthetic_source: bool


def _git_identity(root: Path) -> tuple[str, str]:
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        tree = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise TrustedLambdaAssetError(
            "trusted Lambda release identity must be supplied or resolved from Git"
        ) from error
    return commit, tree


def resolve_trusted_lambda_asset_metadata(
    repository_root: Path,
    *,
    account: str | None,
    allow_synthetic_source: bool = False,
    expected_commit: str | None = None,
    expected_tree: str | None = None,
) -> ResolvedTrustedLambdaAsset:
    """Resolve the exact ZIP/hash pair or one impossible-account test escape."""

    root = Path(repository_root).resolve(strict=True)
    artifact = root / "build" / "trusted-lambda"
    artifact_markers = (
        artifact / "MANIFEST.json",
        artifact / "trusted-lambda.zip",
        artifact / "ASSET.sha256",
        artifact / "SHA256SUMS",
    )
    if any(path.exists() or path.is_symlink() for path in artifact_markers):
        if expected_commit is None or expected_tree is None:
            git_commit, git_tree = _git_identity(root)
            expected_commit = expected_commit or git_commit
            expected_tree = expected_tree or git_tree
        try:
            manifest = verify_trusted_lambda_artifact(
                artifact,
                root / "lambda",
                expected_commit=expected_commit,
                expected_tree=expected_tree,
            )
        except (ContractError, OSError, UnicodeError) as error:
            raise TrustedLambdaAssetError(str(error)) from error
        archive = artifact / manifest.archive_name
        return ResolvedTrustedLambdaAsset(
            path=str(archive),
            asset_hash=manifest.archive_sha256,
            synthetic_source=False,
        )

    if allow_synthetic_source and account == SYNTHETIC_ACCOUNT:
        source = root / "lambda"
        if not source.is_dir() or source.is_symlink():
            raise TrustedLambdaAssetError("synthetic Lambda source root is invalid")
        return ResolvedTrustedLambdaAsset(
            path=str(source), asset_hash=None, synthetic_source=True
        )

    raise TrustedLambdaAssetError(
        "build/trusted-lambda/trusted-lambda.zip is missing; run "
        "scripts/build-trusted-lambda-asset.sh build"
    )


def resolve_trusted_lambda_asset(
    repository_root: Path,
    *,
    account: str | None,
    allow_synthetic_source: bool = False,
    expected_commit: str | None = None,
    expected_tree: str | None = None,
) -> str:
    """Compatibility wrapper returning only the resolved deployment path."""

    return resolve_trusted_lambda_asset_metadata(
        repository_root,
        account=account,
        allow_synthetic_source=allow_synthetic_source,
        expected_commit=expected_commit,
        expected_tree=expected_tree,
    ).path
