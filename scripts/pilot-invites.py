#!/usr/bin/env python3
"""Issue or revoke one opaque Personal Operator pilot invitation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
LAMBDA_ROOT = ROOT / "lambda"
if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))

from control.invites import DynamoPilotInvites  # noqa: E402


REQUIRED_REGION = "eu-west-1"


def _production_table(*, table_name: str, region: str):
    import boto3
    from botocore.config import Config

    return boto3.resource(
        "dynamodb",
        region_name=region,
        config=Config(retries={"max_attempts": 0}),
    ).Table(table_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage digest-only Personal Operator pilot invitations.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("issue", "revoke"):
        command = subcommands.add_parser(name)
        command.add_argument("--table", required=True)
        command.add_argument(
            "--region",
            default=REQUIRED_REGION,
            choices=[REQUIRED_REGION],
        )
        if name == "issue":
            command.add_argument("--ttl-seconds", type=int, default=7 * 24 * 60 * 60)
        else:
            command.add_argument("--token", required=True)
    return parser


def main(
    argv=None,
    *,
    table_factory=None,
    stdout=None,
    now=None,
    random_bytes=None,
    conditional_failure_types=(),
) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    factory = table_factory or _production_table
    table = factory(table_name=args.table, region=args.region)
    invites = DynamoPilotInvites(
        table,
        now=now,
        random_bytes=random_bytes,
        conditional_failure_types=conditional_failure_types,
    )
    if args.command == "issue":
        # Stdout is the intentional one-time bearer handoff. The token is not
        # written to DynamoDB or emitted through application logging/metrics.
        output.write(invites.issue(ttl_seconds=args.ttl_seconds).token + "\n")
        return 0
    revoked = invites.revoke(args.token)
    output.write(("revoked" if revoked else "unavailable") + "\n")
    return 0 if revoked else 2


if __name__ == "__main__":
    raise SystemExit(main())
