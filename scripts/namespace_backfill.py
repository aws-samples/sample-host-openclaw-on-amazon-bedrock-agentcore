#!/usr/bin/env python3
"""Store each user's current storage namespace on its route (OPERATOR TOOL). Dry run by default.

    scripts/namespace_backfill.py [--user-id user_<16 hex>] [--limit N] [--apply]
                                  [--region R] [--env-suffix S]

Stage 2 of the namespace migration (docs/operations.md, "Namespace migration"). For every
USER#<id>/SESSION without a `namespace` attribute, with --apply, in this order:

  1  NS#<namespace>/OWNER {userId}, unless it exists naming this user. A record naming another
     user is a conflict: the user is reported and left alone.
  2  SESSION.namespace = the namespace the route already uses (the runtime actor with ':' as
     '_'), conditional on the route snapshot and on no invocation in flight.

No object, secret or schedule moves: the stored value is the one the Router, the Cron Lambda and
the bridge derive today, so no turn changes. A route that is busy, frozen or changed is skipped;
run the tool again later. The runtime actor must be confirmed by its ACTOR#<actor>/OWNER record,
which is outside the user's partition; a route it does not confirm is reported and skipped. A
route that already stores a namespace is reported as already stored when NS#<namespace>/OWNER
names the user, and as a conflict otherwise.
Requires the release that reads stored namespaces on the Router, the Cron Lambda and the bridge.

Output names users by internal user ID only: no actor or namespace. Exit status: 0 done or dry
run, 1 some routes were busy or a call failed (re-run to continue), 2 bad arguments, 3 some
users need attention first (conflict, unconfirmed actor, or a route that is not ACTIVE).
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "lambda" / "shared" / "python"))

from delete_user_data import ACTOR_RE, USER_ID_RE, Aws, Failed, resolve_deployment  # noqa: E402
from env_suffix import env_suffix  # noqa: E402
import runtime_routing  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


def _code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code", "")


def _get(table, pk, sk):
    return table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True).get("Item")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def confirmed_runtime_actor(table, user_id, session):
    """The route's actor when its ACTOR# owner record names this user as its runtime actor, else None."""
    actor = session.get("actorId")
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        return None
    owner = _get(table, f"ACTOR#{actor}", "OWNER") or {}
    # A linked channel's owner record names the runtime actor it belongs to.
    if (owner.get("userId") != user_id or owner.get("actorId") != actor
            or owner.get("runtimeActorId") not in (None, actor)):
        return None
    return actor


def sessions(table, user_id=None):
    """SESSION items, one user or every user."""
    if user_id:
        item = _get(table, f"USER#{user_id}", "SESSION")
        yield from ([item] if item else [])
        return
    request = {"FilterExpression": "begins_with(PK, :user) AND SK = :session",
               "ExpressionAttributeValues": {":user": "USER#", ":session": "SESSION"}, "ConsistentRead": True}
    start = None
    while True:
        page = table.scan(**request, **({"ExclusiveStartKey": start} if start else {}))
        yield from page.get("Items", [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return


def claim_owner(table, namespace, user_id, apply):
    """Make NS#<namespace>/OWNER name user_id. Returns "exists", "created", "would create" or "conflict"."""
    owner = _get(table, f"NS#{namespace}", "OWNER")
    if owner:
        return "exists" if owner.get("userId") == user_id else "conflict"
    if not apply:
        return "would create"
    try:
        table.put_item(Item={"PK": f"NS#{namespace}", "SK": "OWNER", "userId": user_id, "createdAt": _now(),
                             "source": "backfill"},
                       ConditionExpression="attribute_not_exists(PK)")
    except Exception as error:
        if _code(error) != "ConditionalCheckFailedException":
            raise
        owner = _get(table, f"NS#{namespace}", "OWNER") or {}
        return "exists" if owner.get("userId") == user_id else "conflict"
    return "created"


def store_namespace(table, route):
    """SESSION.namespace = route.namespace while the route is unchanged and idle. False when it is not."""
    condition, names, values = runtime_routing._condition(route)
    names.update({"#inFlight": "inFlight", "#admissions": "activeAdmissions"})
    try:
        table.update_item(
            Key=route.key, UpdateExpression="SET #namespace = :stored",
            ConditionExpression=(f"({condition}) AND (attribute_not_exists(#inFlight) OR #inFlight = :zero)"
                                 " AND attribute_not_exists(#admissions)"),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":stored": route.namespace, ":zero": 0},
        )
    except Exception as error:
        if _code(error) != "ConditionalCheckFailedException":
            raise
        return False
    return True


def backfill_user(table, session, apply):
    """One user's outcome: stored, would store, already stored, busy, inactive, unconfirmed or conflict."""
    user_id = session["PK"].split("#", 1)[1]
    if "namespace" in session:
        stored = session["namespace"]
        owner = _get(table, f"NS#{stored}", "OWNER") if isinstance(stored, str) and stored else None
        return "already stored" if (owner or {}).get("userId") == user_id else "conflict"
    try:
        route = runtime_routing.route_from_record(session, user_id)
    except runtime_routing.RouteUnavailable:
        return "inactive"
    if (int(session.get("inFlight") or 0) or session.get("activeAdmissions")):
        return "busy"
    if confirmed_runtime_actor(table, user_id, session) != route.actor_id:
        return "unconfirmed"
    owner = claim_owner(table, route.namespace, user_id, apply)
    if owner == "conflict":
        return "conflict"
    if not apply:
        return "would store"
    return "stored" if store_namespace(table, route) else "busy"


OUTCOMES = ("stored", "would store", "already stored", "busy", "inactive", "unconfirmed", "conflict")
EXPLAIN = {
    "busy": "an invocation was in flight or the route changed; run the tool again",
    "inactive": "the route is not ACTIVE (frozen, migrating or invalid); finish that operation first",
    "unconfirmed": "no ACTOR# owner record confirms the route's actor as this user's runtime actor; reconcile",
    "conflict": "the NS#<namespace> owner record is missing or names another user; reconcile before migrating "
                "this user",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="namespace_backfill.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Store each user's current namespace on its route. Nothing is changed unless --apply is given.",
        epilog="exit status: 0 done or dry run, 1 re-run to continue, 2 bad arguments, 3 reconciliation needed\n"
               "see docs/operations.md, 'Namespace migration'")
    parser.add_argument("--user-id", help="only this user, user_<16 hex>")
    parser.add_argument("--limit", type=int, help="process at most N routes that need a stored namespace")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is changed")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION, AWS_DEFAULT_REGION, then cdk.json)")
    parser.add_argument("--env-suffix", help="deployment environment suffix (default: OPENCLAW_ENV_SUFFIX)")
    return parser


def _region(cli_region):
    region = cli_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    try:
        return json.loads((REPO_ROOT / "cdk.json").read_text())["context"].get("region")
    except (OSError, ValueError, KeyError):
        return None


def main(argv=None, aws=None, out=None):
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.user_id and not USER_ID_RE.fullmatch(args.user_id):
        parser.error("--user-id must be user_<16 hex>")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    raw_suffix = args.env_suffix if args.env_suffix is not None else os.environ.get("OPENCLAW_ENV_SUFFIX", "")
    try:
        suffix = env_suffix({"OPENCLAW_ENV_SUFFIX": raw_suffix})
    except ValueError as error:
        parser.error(str(error))
    region = _region(args.region)
    if not region:
        parser.error("cannot determine the AWS region; pass --region")
    aws = aws or Aws(region)
    mode = "APPLY" if args.apply else "DRY RUN, nothing is changed (add --apply to write)"
    print(f"namespace_backfill: {mode}; region {region}, environment suffix {suffix or '(none)'}", file=out)
    counts = {outcome: [] for outcome in OUTCOMES}
    try:
        table = aws.table(resolve_deployment(aws.cfn, suffix).identity_table)
        processed = 0
        for session in sessions(table, args.user_id):
            if not isinstance(session.get("PK"), str) or not USER_ID_RE.fullmatch(session["PK"].split("#", 1)[-1]):
                continue  # not an internal user ID (a test or hand-made record)
            if args.limit is not None and processed >= args.limit and "namespace" not in session:
                break
            outcome = backfill_user(table, session, args.apply)
            counts[outcome].append(session["PK"].split("#", 1)[1])
            processed += "namespace" not in session
    except Exception as error:
        detail = _code(error) or (str(error) if isinstance(error, Failed) else type(error).__name__)
        print(f"FAILED: {detail}. Re-run the same command to continue.", file=out)
        return EXIT_FAILED
    for outcome in OUTCOMES:
        users = counts[outcome]
        if users:
            reason = f" ({EXPLAIN[outcome]})" if outcome in EXPLAIN else ""
            listed = ", ".join(users) if outcome in EXPLAIN else ""
            print(f"{outcome}: {len(users)}{reason}{': ' + listed if listed else ''}", file=out)
    if not any(counts.values()):
        print("no routes found", file=out)
    if counts["conflict"] or counts["unconfirmed"] or counts["inactive"]:
        return EXIT_REFUSED
    return EXIT_FAILED if counts["busy"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
