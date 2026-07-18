"""Canonical release-artifact adapters used by offline and staging scripts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from release_tools.contracts import (
    ContractError,
    RuntimeContextV3,
    canonical_json_bytes,
    read_regular_bytes,
)


def build_cdk_context(
    config_path: Path,
    runtime_context_path: Path,
    *,
    source_commit: str,
    account: str,
    region: str,
    runtime_image_uri: str,
) -> dict[str, Any]:
    """Merge one strict RuntimeContextV3 into ordinary CDK configuration."""

    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("cdk.json is unavailable or invalid") from error
    if not isinstance(config, dict) or not isinstance(config.get("context"), dict):
        raise ContractError("cdk.json context is invalid")
    runtime = RuntimeContextV3.from_bytes(
        read_regular_bytes(Path(runtime_context_path))
    )
    if (
        runtime.source_commit,
        runtime.account,
        runtime.region,
        runtime.runtime_image_uri,
    ) != (source_commit, account, region, runtime_image_uri):
        raise ContractError("runtime context is not bound to this release")
    context = dict(config["context"])
    context.update(
        {
            "runtime_source_commit": runtime.source_commit,
            "runtime_id": runtime.runtime_id,
            "runtime_endpoint_id": runtime.runtime_endpoint_id,
            "runtime_endpoint_name": runtime.runtime_endpoint_name,
            "runtime_version": runtime.runtime_version,
            "runtime_arn": runtime.runtime_arn,
            "runtime_image_uri": runtime.runtime_image_uri,
        }
    )
    return context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    context = subparsers.add_parser("cdk-context")
    context.add_argument("--config", type=Path, required=True)
    context.add_argument("--runtime-context", type=Path, required=True)
    context.add_argument("--source-commit", required=True)
    context.add_argument("--account", required=True)
    context.add_argument("--region", required=True)
    context.add_argument("--runtime-image-uri", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = build_cdk_context(
            args.config,
            args.runtime_context,
            source_commit=args.source_commit,
            account=args.account,
            region=args.region,
            runtime_image_uri=args.runtime_image_uri,
        )
    except (ContractError, OSError) as error:
        print(f"release asset gate: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
