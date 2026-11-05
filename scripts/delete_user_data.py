#!/usr/bin/env python3
"""Delete one user's data from an OpenClaw deployment (OPERATOR TOOL). Dry run by default.

    scripts/delete_user_data.py (--actor <channel:id> | --user-id user_<16 hex>)
                                [--apply] [--force-delete-secrets] [--region R] [--env-suffix S]

Without --apply nothing is changed: each step lists what it would delete. With --apply the
steps run in this order. Each one lists, then deletes, so a run that stops part-way can be
re-run with the same arguments to finish the job:

   1  resolve the user, its linked actors, namespace, session and schedules
   2  revoke: delete ALLOW# for each linked actor, set PROFILE.accessStatus = DISABLED
   3  freeze the route and stop the AgentCore session (refused while admissions are held)
   4  EventBridge schedules, then the CRON# records
   5  S3: every version and delete marker under <ns>/ and _system/<ns>/, then a check
   6  secrets: openclaw/user/<ns>/* and openclaw/cognito-user/<pool>/<ns>
   7  the Cognito user
   8  token-usage rows
   9  /openclaw/container log streams
  10  identity records (usage items under USAGE#<id> first), last, so an interrupted run can still find everything
  11  what this tool cannot delete

Records outside the user's USER#<id> partition decide which actors and namespaces belong to
the user: CHANNEL#<actor>/PROFILE, ACTOR#<actor>/OWNER, NS#<ns>/OWNER, or the deterministic
V2 user ID. A back-reference, CRON# record or SESSION field in the partition is only acted on
outside it when one of those records agrees, so a stale or inconsistent item cannot widen the
deletion to another user's data.

Output names the user by internal user ID only: no actor, namespace, key or resource name.
Resource names come from the CloudFormation outputs of the (suffixed) stacks.
Exit status: 0 done or dry run, 1 a step failed (re-run to continue), 2 bad arguments,
3 refused. See docs/operations.md, "Deleting a user's data".
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "lambda" / "shared" / "python"))

from env_suffix import env_suffix, with_suffix  # noqa: E402
import runtime_routing  # noqa: E402

# Same format check as scripts/manage-allowlist.sh validate_channel_key.
ACTOR_RE = re.compile(r"[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.@+-]{1,128}")
USER_ID_RE = re.compile(r"user_[0-9a-f]{16}")
OPAQUE_NAMESPACE_RE = re.compile(r"ns_[a-z2-7]{26}")  # a stored opaque namespace
BIND_CODE_RE = re.compile(r"[A-Za-z0-9]{1,64}")
# bridge/skills/eventbridge-cron/common.js MAX_NAME_NAMESPACE.
SCHEDULE_NAMESPACE_MAX = 64 - len("openclaw-") - 9
RECOVERY_WINDOW_DAYS = 7
S3_BATCH = 1000
S3_PASSES = 3
# The bridge writes this group under a fixed name, so every deployment in the
# account and region shares it (stacks/health_alarms.py).
CONTAINER_LOG_GROUP = "/openclaw/container"
OUTPUTS = {  # resource: (stack base name, CloudFormation output key)
    "identity_table": ("OpenClawRouter", "IdentityTableName"),
    "token_table": ("OpenClawTokenMonitoring", "TokenUsageTableName"),
    "bucket": ("OpenClawAgentCore", "UserFilesBucketName"),
    "user_pool": ("OpenClawSecurity", "CognitoUserPoolId"),
}
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
NOT_DELETABLE = (
    "CloudTrail event history (STS session, secret and schedule names): 90 days; "
    "a dedicated or organization trail keeps its own copy",
    "Step Functions execution history of async scheduled-task runs: 90 days after each run",
    "events in the shared Router, Cron, token-metrics and Bedrock invocation log groups, "
    "until their retention ends (CloudWatch Logs cannot delete single events)",
    "CloudWatch metrics with an ActorId dimension published before #230: 15 months",
    "point-in-time recovery copies of the identity and token tables: 35 days",
    "AgentCore session storage, until it expires (about 14 days idle; there is no delete API)",
    "mentions of the person inside other identities' data, such as Telegram group transcripts",
)


class Refused(Exception):
    """The tool will not continue; the message says why and what to do."""


class Failed(Exception):
    """A step could not finish; re-running the same command continues."""


def _code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code", "")


def _count(number, noun, plural=None):
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def legacy_namespace(actor):
    """Today's namespace: the runtime actor with every ':' replaced by '_'."""
    return actor.replace(":", "_")


def derived_user_id(actor):
    """V2 user ID of an actor (lambda/shared/python/runtime_enrollment.py)."""
    return f"user_{hashlib.sha256(actor.encode()).hexdigest()[:16]}"


def schedule_prefixes(namespace):
    """(name prefix, needs an owner check) per schedule-name form (buildScheduleName in the cron skill)."""
    if len(namespace) <= SCHEDULE_NAMESPACE_MAX:
        return [(f"openclaw-{namespace}-", False)]
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:12]
    # The pre-digest form cuts the namespace, so another namespace can share that prefix.
    return [(f"openclaw-{namespace[:SCHEDULE_NAMESPACE_MAX - 13]}-{digest}-", False),
            (f"openclaw-{namespace[:SCHEDULE_NAMESPACE_MAX]}-", True)]


def _ignore(error, *codes):
    if _code(error) not in codes:
        raise error


# ---------------------------------------------------------------------------
# DynamoDB helpers (boto3 Table resources)
# ---------------------------------------------------------------------------

def _get(table, pk, sk):
    return table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True).get("Item")


def _query(table, pk):
    items, start = [], None
    while True:
        request = {"KeyConditionExpression": "PK = :pk", "ExpressionAttributeValues": {":pk": pk},
                   "ConsistentRead": True}
        if start:
            request["ExclusiveStartKey"] = start
        page = table.query(**request)
        items += page.get("Items", [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


# ---------------------------------------------------------------------------
# Step 1: resolve
# ---------------------------------------------------------------------------

@dataclass
class User:
    user_id: str
    partition: dict                  # SK -> item of USER#<id>
    runtime_actor: str = ""          # verified; "" when unknown
    linked: list = field(default_factory=list)   # verified actors other than the runtime actor
    unverified_backrefs: int = 0     # back-references no record outside the partition confirms
    namespaces: list = field(default_factory=list)
    finishing: bool = False          # an earlier run got into step 10; only identity records are left


def _owners(table, actor):
    """The user IDs that records outside a user's partition give this actor, and those records."""
    records = {"CHANNEL": _get(table, f"CHANNEL#{actor}", "PROFILE"), "ACTOR": _get(table, f"ACTOR#{actor}", "OWNER")}
    return {r["userId"] for r in records.values() if r and r.get("userId")}, records


def _ownership(table, actor, user_id):
    """("own" | "other" | None, records): do the records outside the partition tie actor to user?"""
    owners, records = _owners(table, actor)
    if owners - {user_id}:
        return "other", records
    if user_id in owners or derived_user_id(actor) == user_id:
        return "own", records
    return None, records


def find_user_id(table, actor=None, user_id=None):
    """The internal user ID for --actor or --user-id, or None when no record names one."""
    if user_id:
        return user_id
    owners, _ = _owners(table, actor)
    if len(owners) > 1:
        raise Refused("this actor's CHANNEL# and ACTOR# records name different users; reconcile first")
    if owners:
        return owners.pop()
    candidate = derived_user_id(actor)
    if any(_get(table, f"USER#{candidate}", sk) for sk in ("PROFILE", "SESSION")):
        return candidate
    return None


def resolve_namespaces(table, user, session):
    """The namespaces that hold this user's data.

    Today a namespace is the runtime actor with ':' as '_'. A stored opaque namespace
    (SESSION.namespace or NS_MIGRATION, with an NS#<ns>/OWNER record naming the user) is
    handled too. A stored value counts only when its NS#<ns>/OWNER record agrees. The side
    a finished migration left (`to` after a rollback, `from` after the cleanup) is ignored:
    scripts/namespace_cutover.py deleted its data, and its NS# record last.
    """
    namespaces = [legacy_namespace(user.runtime_actor)] if user.runtime_actor else []
    migration = user.partition.get("NS_MIGRATION") or {}
    # A finished migration (scripts/namespace_cutover.py) deleted the side it left, NS# record last.
    finished = {"ROLLED_BACK": "to", "CLEANED": "from"}.get(migration.get("state"))
    stored = {(session or {}).get("namespace"), *(migration.get(side) for side in ("from", "to") if side != finished)}
    for namespace in sorted(value for value in stored if isinstance(value, str) and value):
        if namespace in namespaces:
            continue
        if not OPAQUE_NAMESPACE_RE.fullmatch(namespace):
            raise Refused("a stored namespace has an unexpected format; reconcile first")
        owner = _get(table, f"NS#{namespace}", "OWNER")
        if not owner or owner.get("userId") != user.user_id:
            raise Refused("a stored namespace is not owned by this user (NS# record); reconcile first")
        namespaces.append(namespace)
    return namespaces


def resolve(table, user_id, actor=None):
    partition = {item["SK"]: item for item in _query(table, f"USER#{user_id}")}
    user = User(user_id, partition)
    actors = {sk.split("#", 1)[1] for sk in partition if sk.startswith("CHANNEL#")} | ({actor} if actor else set())
    runtime_candidates = {value for value in ((partition.get("SESSION") or {}).get("actorId"),
                                              (partition.get("PROFILE") or {}).get("enrollmentActorId")) if value}
    verified = []
    for candidate in sorted(actors):
        result, records = _ownership(table, candidate, user_id) if ACTOR_RE.fullmatch(candidate) else (None, {})
        if result != "own":
            # Not confirmed outside the partition: only the back-reference itself is deleted (step 10).
            user.unverified_backrefs += 1
            continue
        verified.append(candidate)
        # A linked actor's owner record, outside the partition, names the runtime actor.
        runtime_of = (records.get("ACTOR") or {}).get("runtimeActorId")
        if runtime_of:
            runtime_candidates.add(runtime_of)
        elif not runtime_candidates and candidate == actor and records.get("ACTOR"):
            runtime_candidates.add(candidate)  # the user's partition is gone; the actor's own records remain
    if len(runtime_candidates) > 1:
        raise Refused("SESSION, PROFILE and ACTOR# records disagree on the runtime actor; reconcile first")
    if runtime_candidates:
        runtime = runtime_candidates.pop()
        result = _ownership(table, runtime, user_id)[0] if ACTOR_RE.fullmatch(runtime) else "other"
        if result == "other":
            raise Refused("the runtime actor is registered to another user; reconcile first")
        if result == "own":
            user.runtime_actor = runtime
    user.linked = [a for a in verified if a != user.runtime_actor]
    if not user.runtime_actor:
        # SESSION is deleted only in step 10, after steps 2-9 succeeded, and step 2 stamps PROFILE.
        profile = partition.get("PROFILE") or {}
        if "SESSION" not in partition and profile.get("deletionStartedAt"):
            user.finishing = True
        elif partition:
            raise Refused("no record outside the user's partition proves its runtime actor, so its namespace "
                          "cannot be checked; reconcile the identity records first")
    else:
        user.namespaces = resolve_namespaces(table, user, partition.get("SESSION"))
    return user


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------

@dataclass
class Deployment:
    identity_table: str
    token_table: str
    bucket: str
    user_pool: str
    schedule_group: str


@dataclass
class Run:
    aws: object
    deployment: Deployment
    user: User
    apply: bool
    force_secrets: bool
    out: object
    table: object
    blocked: str = ""
    kept: list = field(default_factory=list)

    def say(self, step, name, text):
        print(f"[{step}] {name}: {text}", file=self.out)

    def did(self, verb, past):
        return past if self.apply else f"would {verb}"


def resolve_deployment(cfn, suffix):
    """Resource names from the stacks' CloudFormation outputs, as scripts/deploy.sh reads them."""
    environ = {"OPENCLAW_ENV_SUFFIX": suffix}
    outputs, names = {}, {}
    for resource, (base, key) in OUTPUTS.items():
        stack = with_suffix(base, environ)
        if stack not in outputs:
            try:
                described = cfn.describe_stacks(StackName=stack)["Stacks"][0]
            except Exception as error:
                raise Failed(f"cannot read the {stack} stack ({_code(error) or type(error).__name__})") from None
            outputs[stack] = {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}
        if not outputs[stack].get(key):
            raise Failed(f"the {stack} stack has no {key} output")
        names[resource] = outputs[stack][key]
    return Deployment(schedule_group=with_suffix("openclaw-cron", environ), **names)


# ---------------------------------------------------------------------------
# Steps 2-10
# ---------------------------------------------------------------------------

def step_revoke(run):
    user, table = run.user, run.table
    allows = [a for a in (user.runtime_actor, *user.linked) if a and _get(table, f"ALLOW#{a}", "ALLOW")]
    profile = user.partition.get("PROFILE")
    stamp = bool(profile) and not (profile.get("accessStatus") == "DISABLED" and profile.get("deletionStartedAt"))
    if run.apply:
        for actor in allows:
            table.delete_item(Key={"PK": f"ALLOW#{actor}", "SK": "ALLOW"})
        if stamp:
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # As `manage-allowlist.sh disable`; deletionStartedAt lets a re-run recognise its own work.
            table.update_item(
                Key={"PK": f"USER#{user.user_id}", "SK": "PROFILE"},
                UpdateExpression="SET accessStatus = :disabled, disabledAt = if_not_exists(disabledAt, :now), "
                                 "deletionStartedAt = if_not_exists(deletionStartedAt, :now)",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":disabled": "DISABLED", ":now": now},
            )
    access = (run.did("set PROFILE.accessStatus = DISABLED", "set PROFILE.accessStatus = DISABLED") if stamp
              else "PROFILE already disabled" if profile else "no PROFILE record")
    run.say(2, "revoke", f"{run.did('delete', 'deleted')} {_count(len(allows), 'ALLOW# record')}; {access}")


def step_session(run):
    user, table = run.user, run.table
    session = _get(table, f"USER#{user.user_id}", "SESSION")
    if not session:
        run.say(3, "session", "no SESSION record; nothing to stop")
        return
    held = max(len(session.get("activeAdmissions") or ()), int(session.get("inFlight") or 0))
    status = session.get("routeStatus")
    if held or status not in ("ACTIVE", "DRAINING"):
        reason = (f"the route holds {_count(held, 'invocation admission')}: a turn may still be running "
                  "(a scheduled task can run for 55 minutes) or ended with an unknown outcome. Let running turns "
                  "finish, reconcile unknown outcomes (docs/operations.md, Inspect sessions), then re-run"
                  if held else f"the route is {status}; finish or roll back that operation first")
        if run.apply:
            raise Refused(f"step 3: {reason}. The user is already disabled, so no new turn starts; "
                          "nothing after step 2 was changed")
        run.blocked = reason
        run.say(3, "session", f"--apply would stop here: {reason}")
        return
    try:
        # Validate the route as the Router does; a frozen route is checked as if it were active.
        route = runtime_routing.route_from_record({**session, "routeStatus": "ACTIVE"}, user.user_id)
    except runtime_routing.RouteUnavailable as error:
        raise Refused(f"step 3: the SESSION record is invalid ({error})") from None
    if not run.apply:
        action = "freeze the route and stop" if status == "ACTIVE" else "stop (the route is already frozen)"
        run.say(3, "session", f"would {action} the AgentCore session")
        return
    if status == "ACTIVE":
        try:
            runtime_routing.freeze_route(table, route)
        except runtime_routing.RouteUnavailable:
            raise Refused("step 3: the route changed or took an admission while being frozen; re-run") from None
    try:
        run.aws.agentcore.stop_runtime_session(**route.invocation())  # as tests/e2e/session.py
        stopped = "stopped the AgentCore session"
    except Exception as error:
        _ignore(error, "ResourceNotFoundException")
        stopped = "the AgentCore session was not running"
    run.say(3, "session", f"froze the route; {stopped}" if status == "ACTIVE" else stopped)


def _owned_schedule(run, name):
    """For the collision-prone schedule-name form: the schedule's input must name this user."""
    try:
        detail = run.aws.scheduler.get_schedule(Name=name, GroupName=run.deployment.schedule_group)
    except Exception as error:
        _ignore(error, "ResourceNotFoundException")
        return False
    try:
        payload = json.loads(detail.get("Target", {}).get("Input") or "{}")
    except ValueError:
        return False
    return (payload.get("actorId") in {run.user.runtime_actor, *run.user.linked}
            or payload.get("userId") == run.user.user_id)


def step_schedules(run):
    user, scheduler, group = run.user, run.aws.scheduler, run.deployment.schedule_group
    cron = [item for sk, item in sorted(user.partition.items()) if sk.startswith("CRON#")]
    prefixes = [p for namespace in user.namespaces for p in schedule_prefixes(namespace)]
    names, outside = set(), 0
    for item in cron:
        name = item.get("ebName")
        if not isinstance(name, str) or not name:
            continue
        check = next((needs_check for prefix, needs_check in prefixes if name.startswith(prefix)), None)
        if check is None or (check and not _owned_schedule(run, name)):
            outside += 1  # never follow a CRON# record outside this user's schedule names
        else:
            names.add(name)
    for prefix, check in prefixes:
        token = None
        while True:
            page = scheduler.list_schedules(GroupName=group, NamePrefix=prefix,
                                            **({"NextToken": token} if token else {}))
            names |= {s["Name"] for s in page.get("Schedules", [])
                      if s["Name"].startswith(prefix) and (not check or _owned_schedule(run, s["Name"]))}
            token = page.get("NextToken")
            if not token:
                break
    gone = 0
    if run.apply:
        for name in sorted(names):
            try:
                scheduler.delete_schedule(Name=name, GroupName=group)
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
                gone += 1
        for item in cron:
            run.table.delete_item(Key={"PK": f"USER#{user.user_id}", "SK": item["SK"]})
    if outside:
        run.kept.append(f"{_count(outside, 'CRON# record')} named a schedule outside this user's schedule names; "
                        "that schedule was left alone")
    already = f" ({gone} already gone)" if gone else ""
    run.say(4, "schedules", f"{run.did('delete', 'deleted')} {_count(len(names) - gone, 'schedule')}{already}, "
                            f"then {_count(len(cron), 'CRON# record')}")


def _list_versions(s3, bucket, prefix):
    found, markers = [], {}
    while True:
        page = s3.list_object_versions(Bucket=bucket, Prefix=prefix, **markers)
        for kind in ("Versions", "DeleteMarkers"):
            found += [(kind, entry["Key"], entry["VersionId"], entry.get("Size", 0))
                      for entry in page.get(kind, []) if entry["Key"].startswith(prefix)]
        if not page.get("IsTruncated"):
            return found
        markers = {"KeyMarker": page["NextKeyMarker"]}
        if page.get("NextVersionIdMarker"):
            markers["VersionIdMarker"] = page["NextVersionIdMarker"]


def step_s3(run):
    s3, bucket = run.aws.s3, run.deployment.bucket
    prefixes = [p for namespace in run.user.namespaces for p in (f"{namespace}/", f"_system/{namespace}/")]
    if not prefixes:
        run.say(5, "s3", "no namespace; nothing to delete")
        return
    found = [entry for prefix in prefixes for entry in _list_versions(s3, bucket, prefix)]
    versions = sum(1 for entry in found if entry[0] == "Versions")
    summary = (f"{_count(versions, 'object version')} ({sum(e[3] for e in found)} bytes) and "
               f"{_count(len(found) - versions, 'delete marker')} under <namespace>/ and _system/<namespace>/")
    if not run.apply:
        calls = -(-len(found) // S3_BATCH)
        run.say(5, "s3", f"would delete {summary} ({_count(calls, 'DeleteObjects call')}), then check that "
                         "both prefixes are empty")
        return
    for passes in range(1, S3_PASSES + 1):
        for start in range(0, len(found), S3_BATCH):
            batch = [{"Key": key, "VersionId": version} for _, key, version, _ in found[start:start + S3_BATCH]]
            errors = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True}).get("Errors") or []
            if errors:
                codes = ", ".join(sorted({error.get("Code", "?") for error in errors}))
                raise Failed(f"S3 refused {_count(len(errors), 'deletion')} ({codes})")
        # A session that was still stopping may have written more; list again.
        found = [entry for prefix in prefixes for entry in _list_versions(s3, bucket, prefix)]
        if not found:
            extra = f" after {passes} passes" if passes > 1 else ""
            run.say(5, "s3", f"deleted {summary}{extra}; both prefixes are now empty")
            return
    raise Failed(f"{_count(len(found), 'object version')} still under the user's prefixes; check that the "
                 "session is stopped, then re-run")


def _user_secrets(run):
    secrets, found = run.aws.secrets, []
    for namespace in run.user.namespaces:
        prefix, token = f"openclaw/user/{namespace}/", None
        while True:
            page = secrets.list_secrets(Filters=[{"Key": "name", "Values": [prefix]}], IncludePlannedDeletion=True,
                                        **({"NextToken": token} if token else {}))
            # The name filter is a case-insensitive prefix match; check it exactly.
            found += [s for s in page.get("SecretList", []) if s.get("Name", "").startswith(prefix)]
            token = page.get("NextToken")
            if not token:
                break
        name = f"openclaw/cognito-user/{run.deployment.user_pool}/{namespace}"
        try:
            secret = secrets.describe_secret(SecretId=name)
        except Exception as error:
            _ignore(error, "ResourceNotFoundException")
            continue
        if secret.get("Name") == name:
            found.append(secret)
    return found


def step_secrets(run):
    found = _user_secrets(run)
    pending = [s for s in found if s.get("DeletedDate")]
    targets = found if run.force_secrets else [s for s in found if not s.get("DeletedDate")]
    if run.apply:
        options = ({"ForceDeleteWithoutRecovery": True} if run.force_secrets
                   else {"RecoveryWindowInDays": RECOVERY_WINDOW_DAYS})
        for secret in targets:
            try:
                run.aws.secrets.delete_secret(SecretId=secret["ARN"], **options)
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
    if run.force_secrets:
        text = f"{run.did('delete', 'deleted')} {_count(len(targets), 'secret')} without a recovery window"
    else:
        text = (f"{run.did('schedule', 'scheduled')} {_count(len(targets), 'secret')} for deletion with a "
                f"{RECOVERY_WINDOW_DAYS}-day recovery window ({len(pending)} already scheduled)")
        if found:
            run.kept.append(f"{_count(len(found), 'secret')} can be restored until the recovery window ends "
                            "(--force-delete-secrets deletes them at once)")
    run.say(6, "secrets", f"{text}; api-key secrets are named per namespace, so every deployment in this account "
                          "and region shares them")


def step_cognito(run):
    usernames = ([run.user.runtime_actor] if run.user.runtime_actor else []) + [
        n for n in run.user.namespaces if OPAQUE_NAMESPACE_RE.fullmatch(n)]
    if not run.apply:
        run.say(7, "cognito", f"would delete the Cognito user ({_count(len(usernames), 'username')} to try)")
        return
    deleted = 0
    for username in usernames:
        try:
            run.aws.cognito.admin_delete_user(UserPoolId=run.deployment.user_pool, Username=username)
            deleted += 1
        except Exception as error:
            _ignore(error, "UserNotFoundException")
    run.say(7, "cognito", f"deleted {_count(deleted, 'Cognito user')}")


def step_tokens(run):
    table = run.aws.table(run.deployment.token_table)
    keys = ([f"USER#{run.user.runtime_actor}"] if run.user.runtime_actor else []) + [f"USER#{run.user.user_id}"]
    rows = [item for key in keys for item in _query(table, key)]
    if run.apply:
        for row in rows:
            table.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    run.say(8, "token usage", f"{run.did('delete', 'deleted')} {_count(len(rows), 'row')}")


def step_logs(run):
    logs, streams = run.aws.logs, []
    for prefix in [f"{n}-" for n in run.user.namespaces] + [f"{run.user.user_id}-"]:
        token = None
        while True:
            try:
                page = logs.describe_log_streams(logGroupName=CONTAINER_LOG_GROUP, logStreamNamePrefix=prefix,
                                                 **({"nextToken": token} if token else {}))
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
                break
            streams += [s["logStreamName"] for s in page.get("logStreams", []) if s["logStreamName"].startswith(prefix)]
            token = page.get("nextToken")
            if not token:
                break
    if run.apply:
        for name in streams:
            try:
                logs.delete_log_stream(logGroupName=CONTAINER_LOG_GROUP, logStreamName=name)
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
    run.say(9, "container logs", f"{run.did('delete', 'deleted')} {_count(len(streams), 'log stream')} in "
                                 f"{CONTAINER_LOG_GROUP}, which every deployment in this account and region shares")


def _group_records(run):
    """ALLOW#telegram_group:* records whose addedBy is one of this user's Telegram actors."""
    actors = sorted(a for a in (run.user.runtime_actor, *run.user.linked) if a.startswith("telegram:"))
    if not actors:
        return []
    values = {f":a{n}": actor for n, actor in enumerate(actors)}
    request = {"FilterExpression": f"begins_with(PK, :group) AND addedBy IN ({', '.join(values)})",
               "ExpressionAttributeValues": {":group": "ALLOW#telegram_group:", **values}}
    found, start = [], None
    while True:
        page = run.table.scan(**request, **({"ExclusiveStartKey": start} if start else {}))
        found += page.get("Items", [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return found


def identity_plan(user, partition):
    """Keys to delete in order: (PK, SK, outside), where outside means not in the user's partition.

    Each linked actor's records go before its back-reference, and PROFILE goes last, so a
    re-run with --user-id still finds whatever an interrupted run left.
    """
    uid = user.user_id
    plan = []
    for actor in user.linked:
        plan += [(f"CHANNEL#{actor}", "PROFILE", True), (f"ACTOR#{actor}", "OWNER", True),
                 (f"USER#{uid}", f"CHANNEL#{actor}", False)]
    code = (partition.get("BIND_POINTER") or {}).get("code")
    if isinstance(code, str) and BIND_CODE_RE.fullmatch(code):
        plan.append((f"BIND#{code}", "BIND", True))
    # Opaque namespaces, and the legacy one once scripts/namespace_backfill.py stored it.
    plan += [(f"NS#{n}", "OWNER", True) for n in user.namespaces]
    runtime_backref = f"CHANNEL#{user.runtime_actor}" if user.runtime_actor else None
    planned = {(pk, sk) for pk, sk, _ in plan}
    plan += [(f"USER#{uid}", sk, False) for sk in sorted(partition)
             if sk not in ("SESSION", "PROFILE", runtime_backref) and (f"USER#{uid}", sk) not in planned]
    plan.append((f"USER#{uid}", "SESSION", False))
    if user.runtime_actor:
        plan += [(f"CHANNEL#{user.runtime_actor}", "PROFILE", True), (f"ACTOR#{user.runtime_actor}", "OWNER", True),
                 (f"USER#{uid}", runtime_backref, False)]
    plan.append((f"USER#{uid}", "PROFILE", False))
    return plan


def step_identity(run):
    user, table, uid = run.user, run.table, run.user.user_id
    # Read the partition again: earlier steps deleted the CRON# records.
    partition = user.partition if not run.apply else {item["SK"]: item for item in _query(table, f"USER#{uid}")}
    if not run.apply:
        partition = {sk: item for sk, item in partition.items() if not sk.startswith("CRON#")}
    # USAGE#<id>: the daily usage items and the limit override (per_user_daily_cost_usd).
    # Keyed by the user ID itself and never read to resolve the user, so they go first.
    usage = [item["SK"] for item in _query(table, f"USAGE#{uid}")]
    present = []
    for pk, sk, outside in identity_plan(user, partition):
        if not outside:
            if pk == f"USER#{uid}" and sk in partition:
                present.append((pk, sk, outside))
        elif (_get(table, pk, sk) or {}).get("userId") == uid:
            present.append((pk, sk, outside))
    groups = _group_records(run)
    if run.apply:
        for sk in usage:
            table.delete_item(Key={"PK": f"USAGE#{uid}", "SK": sk})
        for pk, sk, outside in present:
            if outside:
                # Only while the record still names this user.
                table.delete_item(Key={"PK": pk, "SK": sk}, ConditionExpression="userId = :user",
                                  ExpressionAttributeValues={":user": uid})
            else:
                table.delete_item(Key={"PK": pk, "SK": sk})
    inside = sum(1 for _, _, outside in present if not outside)
    if user.unverified_backrefs:
        run.kept.append(f"{_count(user.unverified_backrefs, 'back-reference')} named an actor that no record outside "
                        "the partition ties to this user; only the back-reference was deleted")
    for group in groups:
        run.kept.append(f"{group['PK'].split('#', 1)[1]} stays allowlisted; its addedBy names this user "
                        "(remove the group or the attribute separately if needed)")
    run.say(10, "identity", f"{run.did('delete', 'deleted')} {_count(inside, 'item')} under USER#<id> and "
                            f"{_count(len(present) - inside, 'CHANNEL#, ACTOR#, BIND# or NS# record')}, PROFILE last; "
                            f"{run.did('delete', 'deleted')} {_count(len(usage), 'usage item')} under USAGE#<id>; "
                            f"kept {_count(len(groups), 'Telegram group allowlist record')} this user added")


STEPS = (step_revoke, step_session, step_schedules, step_s3, step_secrets, step_cognito, step_tokens,
         step_logs, step_identity)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class Aws:
    """The AWS clients the tool uses; tests pass stubs."""

    def __init__(self, region):
        import boto3
        session = boto3.session.Session(region_name=region)
        self._dynamodb = session.resource("dynamodb")
        for name, service in (("cfn", "cloudformation"), ("s3", "s3"), ("scheduler", "scheduler"),
                              ("secrets", "secretsmanager"), ("cognito", "cognito-idp"), ("logs", "logs"),
                              ("agentcore", "bedrock-agentcore")):
            setattr(self, name, session.client(service))

    def table(self, name):
        return self._dynamodb.Table(name)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="delete_user_data.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Delete one user's data from a deployment. Nothing is changed unless --apply is given.",
        epilog="exit status: 0 done or dry run, 1 a step failed (re-run to continue), 2 bad arguments, "
               "3 refused\nsee docs/operations.md, 'Deleting a user's data'")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--actor", help="any of the user's channel identities, e.g. telegram:123456")
    who.add_argument("--user-id", help="internal user ID, user_<16 hex>")
    parser.add_argument("--apply", action="store_true", help="delete; without it nothing is changed")
    parser.add_argument("--force-delete-secrets", action="store_true",
                        help=f"delete secrets at once instead of with a {RECOVERY_WINDOW_DAYS}-day recovery window")
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


def main(argv=None, aws=None, out=None, today=None):
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.actor and not ACTOR_RE.fullmatch(args.actor):
        parser.error("--actor must be <channel>:<id>, e.g. telegram:123456")
    if args.user_id and not USER_ID_RE.fullmatch(args.user_id):
        parser.error("--user-id must be user_<16 hex>")
    raw_suffix = args.env_suffix if args.env_suffix is not None else os.environ.get("OPENCLAW_ENV_SUFFIX", "")
    try:
        suffix = env_suffix({"OPENCLAW_ENV_SUFFIX": raw_suffix})
    except ValueError as error:
        parser.error(str(error))
    region = _region(args.region)
    if not region:
        parser.error("cannot determine the AWS region; pass --region")
    aws = aws or Aws(region)
    mode = "APPLY" if args.apply else "DRY RUN, nothing is changed (add --apply to delete)"
    print(f"delete_user_data: {mode}; region {region}, environment suffix {suffix or '(none)'}", file=out)
    step = 1
    try:
        deployment = resolve_deployment(aws.cfn, suffix)
        table = aws.table(deployment.identity_table)
        user_id = find_user_id(table, actor=args.actor, user_id=args.user_id)
        user = resolve(table, user_id, args.actor) if user_id else None
        if not user or not (user.partition or user.runtime_actor or user.linked):
            print("[1] resolve: no identity records found; nothing to delete. If an earlier run already removed "
                  "this actor's records, re-run with --user-id.", file=out)
            return EXIT_OK
        run = Run(aws, deployment, user, args.apply, args.force_delete_secrets, out, table)
        channels = sorted({a.split(":", 1)[0] for a in (user.runtime_actor, *user.linked) if a})
        state = ("an earlier run reached step 10, so only identity records are left" if user.finishing
                 else f"{_count(len(user.namespaces), 'namespace')}")
        crons = sum(1 for sk in user.partition if sk.startswith("CRON#"))
        run.say(1, "resolve", f"{user.user_id}: {_count(len(channels), 'channel')} ({', '.join(channels) or 'none'}), "
                              f"{state}, {_count(crons, 'CRON# record')}, {_count(len(user.partition), 'item')} "
                              "under USER#<id>")
        for step, function in enumerate(STEPS, start=2):
            if user.finishing and function is not step_identity:
                continue
            function(run)
        step = 11
        print("[11] not deleted by this tool:", file=out)
        for line in (*run.kept, *NOT_DELETABLE):
            print(f"     - {line}", file=out)
        if run.blocked:
            print(f"--apply would be refused at step 3: {run.blocked}.", file=out)
            return EXIT_REFUSED
        if args.apply:
            day = (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
            print(f"AUDIT: {user.user_id} data deleted {day}", file=out)
        return EXIT_OK
    except Refused as error:
        print(f"REFUSED: {error}", file=out)
        return EXIT_REFUSED
    except Exception as error:
        # Error codes and this tool's own messages only: AWS messages can name resources.
        detail = _code(error) or (str(error) if isinstance(error, Failed) else type(error).__name__)
        print(f"[{step}] FAILED: {detail}. Later steps did not run; re-run the same command to continue.", file=out)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
