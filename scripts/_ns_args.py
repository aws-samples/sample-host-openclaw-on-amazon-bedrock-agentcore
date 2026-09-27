"""Shared CLI parsing for the one-off schedule repair scripts.

Both scripts operate on one Telegram user's schedules. The user's namespace
and current internal userId are supplied at run time (CLI flag or env var)
instead of being hardcoded, so no real identifier lives in the repository.
"""
import argparse
import os
import re

NAMESPACE_RE = re.compile(r"^telegram_(\d+)$")
USER_ID_RE = re.compile(r"^user_[0-9a-f]{16}$")


def parse_args(description, with_stale_ids=False):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--namespace",
        default=os.environ.get("OPENCLAW_NAMESPACE"),
        help="Telegram namespace, e.g. telegram_<telegram_id> "
        "(or set OPENCLAW_NAMESPACE)",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("OPENCLAW_USER_ID"),
        help="Current internal userId, e.g. user_<16 hex> "
        "(or set OPENCLAW_USER_ID)",
    )
    if with_stale_ids:
        parser.add_argument(
            "--stale-id",
            action="append",
            default=[],
            help="Stale userId to replace (repeatable). The namespace itself "
            "is always treated as stale.",
        )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.namespace or not NAMESPACE_RE.match(args.namespace):
        parser.error("--namespace telegram_<telegram_id> is required")
    if not args.user_id or not USER_ID_RE.match(args.user_id):
        parser.error("--user-id user_<16 hex chars> is required")
    args.telegram_id = NAMESPACE_RE.match(args.namespace).group(1)
    return args
