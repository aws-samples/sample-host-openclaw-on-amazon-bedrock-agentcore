#!/usr/bin/env python3
"""Move one user's storage to a new opaque namespace (OPERATOR TOOL). Dry run by default.

    scripts/namespace_cutover.py --user-id user_<16 hex> --endpoint NAME [--endpoint NAME ...] [--apply]
                                 [--quiesce-minutes 10] [--region R] [--env-suffix S]
    scripts/namespace_cutover.py --user-id user_<16 hex> (--rollback | --cleanup [--hold-days 14]) [--apply]

Stage 3 of the namespace migration (docs/operations.md, "Namespace migration"). The user's route
must already store its namespace (scripts/namespace_backfill.py) and use one of the --endpoint
endpoints: a route keeps the endpoint it enrolled on, and only a bridge image built with stored
namespace support reads the namespace from the route. Without --rollback or --cleanup the tool
runs these steps. Each one checks whether it already happened, so an interrupted run continues
where it stopped when it is run again:

  plan       allocate the new namespace ("ns_" + 26 base32 characters) and write NS#<new>/OWNER
  freeze     route ACTIVE -> MIGRATING; refused while an invocation is in flight or an admission
             is retained (unknown outcome). The Router answers with a maintenance reply.
  stop       stop the AgentCore session, then wait until the listing of the old prefixes has not
             changed for --quiesce-minutes
  copy       copy the current version of every object under <old>/ and _system/<old>/ to the
             same key under the new namespace; older versions stay with the old prefix
  secrets    copy every openclaw/user/<old>/<key> secret to openclaw/user/<new>/<key>
  schedules  recreate each openclaw-<old>-<id> schedule as openclaw-<new>-<id> (created disabled,
             then given its old state), update CRON#.ebName, delete the old schedule. A one-time
             schedule whose time has passed stays disabled.
  flip       SESSION.namespace = new, generation + 1; the route stays MIGRATING
  verify     copy again whatever changed under the old prefixes since the copy, check secrets
             and schedules
  unfreeze   route MIGRATING -> ACTIVE. The bridge creates the Cognito user under the new
             username at the next turn.

--rollback, while the old data is kept. Before the flip: move the schedules back, unfreeze and
delete the new copies. After it: freeze and stop again, copy back the objects written under the
new namespace since the cut-over and the secrets that differ, move the schedules back, flip back
and unfreeze, then delete the new copies, secrets, Cognito user and NS#<new>.
--cleanup, --hold-days (default 14) after the unfreeze: delete every version under the old
prefixes, the old secrets (7-day recovery window), the old Cognito user and its secret, container
log streams named after the old namespace, and NS#<old>. Refused while anything was written under
the old prefixes after the cut-over was verified.

Progress is kept in USER#<id>/NS_MIGRATION. Every run re-checks its namespaces against their
NS#<namespace>/OWNER records, which are outside the user's partition, and against the route.
Output names the user by internal user ID only. Exit status: 0 done or dry run, 1 a step failed
(re-run to continue), 2 bad arguments, 3 refused (the message says why).
"""

import argparse
import base64
import datetime
import email.utils
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "lambda" / "shared" / "python"))

from delete_user_data import (  # noqa: E402
    CONTAINER_LOG_GROUP, USER_ID_RE, Aws, resolve_deployment, schedule_prefixes,
)
from env_suffix import env_suffix  # noqa: E402
from namespace_backfill import _region, confirmed_runtime_actor  # noqa: E402
import runtime_routing  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
OPAQUE_NAMESPACE_RE = runtime_routing.OPAQUE_NAMESPACE
MIGRATION_SK = "NS_MIGRATION"
STATES = ("PLANNED", "FROZEN", "STOPPED", "COPIED", "SECRETS", "SCHEDULES", "FLIPPED", "VERIFIED", "MOVED")
FINISHED = ("ROLLED_BACK", "CLEANED")
ROLLBACK_STEPS = ("freeze", "stop", "copy back", "secrets back", "schedules back", "flip back", "unfreeze",
                  "remove new")
COPY_OBJECT_LIMIT = 5 * 1024 ** 3  # one CopyObject call
S3_BATCH = 1000
POLL_SECONDS = 30
QUIESCE_LIMIT = 6  # give up after this many quiet windows' worth of continuing writes
RECOVERY_WINDOW_DAYS = 7
SCHEDULE_ID_RE = re.compile(r"[a-f0-9]{8}")
SCHEDULE_NAMESPACE_MAX = 64 - len("openclaw-") - 9  # bridge/skills/eventbridge-cron/common.js
SCHEDULE_FIELDS = ("ScheduleExpression", "ScheduleExpressionTimezone", "FlexibleTimeWindow", "Target",
                   "Description", "StartDate", "EndDate", "KmsKeyArn", "ActionAfterCompletion")
ONE_TIME_RE = re.compile(r"at\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)")


class Refused(Exception):
    """The tool will not continue; the message says why and what to do."""


class Failed(Exception):
    """A step could not finish; re-running the same command continues."""


def _code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code", "")


def _ignore(error, *codes):
    if _code(error) not in codes:
        raise error


def _count(number, noun, plural=None):
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


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


def new_namespace():
    """ "ns_" + 26 base32 characters: 128 bits from the OS CSPRNG."""
    return "ns_" + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def build_schedule_name(namespace, schedule_id):
    """bridge/skills/eventbridge-cron/common.js buildScheduleName."""
    if len(namespace) <= SCHEDULE_NAMESPACE_MAX:
        return f"openclaw-{namespace}-{schedule_id}"
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:12]
    return f"openclaw-{namespace[:SCHEDULE_NAMESPACE_MAX - 13]}-{digest}-{schedule_id}"


def iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass
class Run:
    aws: object
    deployment: object
    table: object
    user_id: str
    apply: bool
    out: object
    now: object            # () -> aware datetime
    monotonic: object      # () -> seconds
    sleep: object          # (seconds) -> None
    quiesce_seconds: int
    hold_days: int
    endpoints: tuple = ()  # endpoints whose bridge image reads the namespace from the route
    session: dict = field(default_factory=dict)
    migration: dict = field(default_factory=dict)
    actor: str = ""
    current: str = ""
    old: str = ""
    new: str = ""
    old_confirmed: bool = True
    new_confirmed: bool = True
    blocked: str = ""

    def say(self, step, text):
        print(f"[{step}] {text}", file=self.out)

    def did(self, verb, past):
        return past if self.apply else f"would {verb}"

    @property
    def key(self):
        return {"PK": f"USER#{self.user_id}", "SK": "SESSION"}

    @property
    def migration_key(self):
        return {"PK": f"USER#{self.user_id}", "SK": MIGRATION_SK}


# ---------------------------------------------------------------------------
# Checks against records outside the user's partition
# ---------------------------------------------------------------------------

def owns(run, namespace):
    owner = _get(run.table, f"NS#{namespace}", "OWNER") or {}
    return owner.get("userId") == run.user_id


def namespace_of_actor(run, namespace):
    """A namespace this user's runtime actor can have: its legacy one or an opaque one."""
    return isinstance(namespace, str) and (
        namespace == runtime_routing.legacy_namespace(run.actor) or bool(OPAQUE_NAMESPACE_RE.fullmatch(namespace)))


def load(run):
    """Read SESSION and NS_MIGRATION and check them against the NS# and ACTOR# records."""
    run.session = _get(run.table, f"USER#{run.user_id}", "SESSION") or {}
    if not run.session:
        raise Refused("the user has no SESSION record")
    run.actor = confirmed_runtime_actor(run.table, run.user_id, run.session) or ""
    if not run.actor:
        raise Refused("no ACTOR# owner record confirms the route's actor as this user's runtime actor; reconcile")
    run.current = run.session.get("namespace")
    if run.current is None:
        raise Refused("the route stores no namespace; run scripts/namespace_backfill.py first")
    if not namespace_of_actor(run, run.current) or not owns(run, run.current):
        raise Refused("the route's namespace is not confirmed by its NS# owner record; reconcile")
    migration = _get(run.table, f"USER#{run.user_id}", MIGRATION_SK) or {}
    run.migration = migration if migration.get("state") not in FINISHED else {}
    if run.migration:
        old, new = run.migration.get("from"), run.migration.get("to")
        if (run.migration.get("state") not in STATES or not namespace_of_actor(run, old)
                or not isinstance(new, str) or not OPAQUE_NAMESPACE_RE.fullmatch(new) or old == new
                or (run.migration.get("rollback") is not None and run.migration["rollback"] not in ROLLBACK_STEPS)):
            raise Refused("the NS_MIGRATION record is not valid; reconcile")
        run.old, run.new = old, new
        run.old_confirmed, run.new_confirmed = owns(run, old), owns(run, new)
        # --cleanup deletes NS#<old> last and --rollback deletes NS#<new> last, after the data
        # under that namespace; a missing record is accepted only at that point of the run.
        if ((not run.old_confirmed and not (run.migration["state"] == "MOVED" and run.current == new))
                or (not run.new_confirmed and not (rollback_done(run, "unfreeze") and run.current == old))):
            raise Refused("an NS# owner record does not confirm the migration's namespaces; reconcile")
        if run.current not in (old, new):
            raise Refused("the route's namespace is neither of the migration's namespaces; reconcile")
    else:
        run.old, run.new = run.current, ""


def reached(run, state):
    recorded = run.migration.get("state")
    return recorded in STATES and STATES.index(recorded) >= STATES.index(state)


def _set_expression(fields, names, values):
    sets = []
    for index, (name, value) in enumerate(sorted(fields.items())):
        names[f"#f{index}"] = name
        values[f":f{index}"] = value
        sets.append(f"#f{index} = :f{index}")
    return sets


def record(run, state, **fields):
    """Advance NS_MIGRATION to state, conditional on the state it had."""
    if not run.apply:
        return
    previous = run.migration.get("state")
    if previous not in STATES or STATES.index(state) != STATES.index(previous) + 1:
        raise Failed(f"cannot record {state} after {previous}; reconcile")
    names, values = {"#state": "state"}, {":state": state, ":previous": previous}
    sets = ["#state = :state", *_set_expression(fields, names, values)]
    try:
        run.table.update_item(Key=run.migration_key, UpdateExpression="SET " + ", ".join(sets),
                              ConditionExpression="#state = :previous",
                              ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except Exception as error:
        _ignore(error, "ConditionalCheckFailedException")
        raise Failed("the NS_MIGRATION record changed during the run; re-run") from None
    run.migration = {**run.migration, "state": state, **fields}


def set_migration_fields(run, **fields):
    """Set NS_MIGRATION fields while its state is the one this run read."""
    if run.apply:
        names, values = {"#state": "state"}, {":state": run.migration.get("state")}
        sets = _set_expression(fields, names, values)
        try:
            run.table.update_item(Key=run.migration_key, UpdateExpression="SET " + ", ".join(sets),
                                  ConditionExpression="#state = :state",
                                  ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        except Exception as error:
            _ignore(error, "ConditionalCheckFailedException")
            raise Failed("the NS_MIGRATION record changed during the run; re-run") from None
    run.migration = {**run.migration, **fields}


def idle(session):
    return not int(session.get("inFlight") or 0) and not session.get("activeAdmissions")


def held_reason(session):
    held = max(len(session.get("activeAdmissions") or ()), int(session.get("inFlight") or 0))
    return (f"the route holds {_count(held, 'invocation admission')}: a turn may still be running (a scheduled "
            "task can run for 55 minutes) or ended with an unknown outcome. Let running turns finish, reconcile "
            "unknown outcomes (docs/operations.md, Inspect sessions), then re-run")


def route_of(run):
    """The route, validated as the Router does; a frozen route is checked as if it were active."""
    try:
        return runtime_routing.route_from_record({**run.session, "routeStatus": "ACTIVE"}, run.user_id)
    except runtime_routing.RouteUnavailable as error:
        raise Refused(f"the SESSION record is invalid ({error})") from None


def set_route(run, update, condition_terms, values, refusal):
    """Conditional SESSION update on the route the run read; refusal when it changed."""
    names = {"#namespace": "namespace", "#routeStatus": "routeStatus", "#generation": "generation",
             "#sessionId": "sessionId", "#inFlight": "inFlight", "#admissions": "activeAdmissions"}
    condition = " AND ".join(["#namespace = :current", "#generation = :generation", "#sessionId = :session",
                              "(attribute_not_exists(#inFlight) OR #inFlight = :zero)",
                              "attribute_not_exists(#admissions)", *condition_terms])
    values = {":current": run.current, ":generation": run.session["generation"],
              ":session": run.session["sessionId"], ":zero": 0, **values}
    used = {name for name in names if name in update or name in condition}
    try:
        run.table.update_item(Key=run.key, UpdateExpression=update, ConditionExpression=condition,
                              ExpressionAttributeNames={k: v for k, v in names.items() if k in used},
                              ExpressionAttributeValues=values)
    except Exception as error:
        _ignore(error, "ConditionalCheckFailedException")
        raise Refused(refusal) from None
    run.session = _get(run.table, f"USER#{run.user_id}", "SESSION") or {}
    run.current = run.session.get("namespace")


def stop_session(run):
    try:
        run.aws.agentcore.stop_runtime_session(**route_of(run).invocation())
    except Exception as error:
        _ignore(error, "ResourceNotFoundException")


# ---------------------------------------------------------------------------
# S3: listings, copies and deletions
# ---------------------------------------------------------------------------

def prefixes(namespace):
    return (f"{namespace}/", f"_system/{namespace}/")


def listing(run, namespace):
    """The current objects under namespace's prefixes and the S3 server time of the last response.

    Objects are {(prefix index, rest of the key): (ETag, size, LastModified)}. LastModified and
    the time both come from S3's clock, so they compare without the operator's clock.
    """
    found, date = {}, None
    for index, prefix in enumerate(prefixes(namespace)):
        token = None
        while True:
            page = run.aws.s3.list_objects_v2(Bucket=run.deployment.bucket, Prefix=prefix,
                                              **({"ContinuationToken": token} if token else {}))
            date = page.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date") or date
            for obj in page.get("Contents", []):
                if obj["Key"].startswith(prefix):
                    found[(index, obj["Key"][len(prefix):])] = (obj.get("ETag", ""), obj.get("Size", 0),
                                                               obj.get("LastModified"))
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
    try:
        server_time = email.utils.parsedate_to_datetime(date) if date else None
    except (TypeError, ValueError):
        server_time = None
    return found, server_time


def digest(found):
    """Fingerprint of a listing: it changes when an object is added, removed or written again."""
    entries = [[index, rest, etag, size, modified.isoformat() if modified else ""]
               for (index, rest), (etag, size, modified) in sorted(found.items())]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def _size(found, keys=None):
    return sum(found[key][1] for key in (found if keys is None else keys))


def too_large(found):
    return [key for key, entry in found.items() if entry[1] > COPY_OBJECT_LIMIT]


def copy_objects(run, source, target, found, keys):
    """Copy the current version of each listed key from source's prefixes to the same key under target's."""
    if too_large({key: found[key] for key in keys}):
        raise Failed("an object is larger than 5 GiB, which one CopyObject call cannot copy; copy it with a "
                     "multipart copy, then re-run")
    if not run.apply:
        return
    bucket = run.deployment.bucket
    for index, rest in keys:
        run.aws.s3.copy_object(Bucket=bucket, Key=prefixes(target)[index] + rest,
                               CopySource={"Bucket": bucket, "Key": prefixes(source)[index] + rest},
                               MetadataDirective="COPY", TaggingDirective="COPY")


def sync(run, source, target, found, full):
    """Copy every listed object when full, else those target lacks or holds with another size. Returns the keys."""
    if full:
        keys = sorted(found)
    else:
        present, _ = listing(run, target)
        keys = sorted(key for key, entry in found.items() if present.get(key, (None, None))[1] != entry[1])
    copy_objects(run, source, target, found, keys)
    return keys


def quiesce(run, namespace):
    """Wait until the listing under namespace's prefixes has not changed for run.quiesce_seconds; its digest."""
    current = digest(listing(run, namespace)[0])
    quiet_since = started = run.monotonic()
    while run.monotonic() - quiet_since < run.quiesce_seconds:
        if run.monotonic() - started > QUIESCE_LIMIT * run.quiesce_seconds:
            raise Failed("objects are still being written under the prefix; check that the session stopped, "
                         "then re-run")
        run.sleep(min(POLL_SECONDS, run.quiesce_seconds - (run.monotonic() - quiet_since)))
        latest = digest(listing(run, namespace)[0])
        if latest != current:
            current, quiet_since = latest, run.monotonic()
    return current


def list_versions(run, prefix):
    s3, found, markers = run.aws.s3, [], {}
    while True:
        page = s3.list_object_versions(Bucket=run.deployment.bucket, Prefix=prefix, **markers)
        for kind in ("Versions", "DeleteMarkers"):
            found += [(entry["Key"], entry["VersionId"]) for entry in page.get(kind, [])
                      if entry["Key"].startswith(prefix)]
        if not page.get("IsTruncated"):
            return found
        markers = {"KeyMarker": page["NextKeyMarker"]}
        if page.get("NextVersionIdMarker"):
            markers["VersionIdMarker"] = page["NextVersionIdMarker"]


def delete_tree(run, namespace):
    """Delete every version and delete marker under namespace's prefixes. Returns how many there were."""
    found = [entry for prefix in prefixes(namespace) for entry in list_versions(run, prefix)]
    if run.apply:
        for start in range(0, len(found), S3_BATCH):
            batch = [{"Key": key, "VersionId": version} for key, version in found[start:start + S3_BATCH]]
            errors = run.aws.s3.delete_objects(Bucket=run.deployment.bucket,
                                               Delete={"Objects": batch, "Quiet": True}).get("Errors") or []
            if errors:
                raise Failed(f"S3 refused {_count(len(errors), 'deletion')}; re-run")
        left = [entry for prefix in prefixes(namespace) for entry in list_versions(run, prefix)]
        if left:
            raise Failed(f"{_count(len(left), 'object version')} appeared while deleting; re-run")
    return len(found)


# ---------------------------------------------------------------------------
# Secrets Manager, Cognito and Scheduler
# ---------------------------------------------------------------------------

def user_secrets(run, namespace, include_pending=False):
    prefix, found, token = f"openclaw/user/{namespace}/", [], None
    while True:
        page = run.aws.secrets.list_secrets(Filters=[{"Key": "name", "Values": [prefix]}],
                                            IncludePlannedDeletion=include_pending,
                                            **({"NextToken": token} if token else {}))
        # The name filter is a case-insensitive prefix match; check it exactly.
        found += [s for s in page.get("SecretList", []) if s.get("Name", "").startswith(prefix)]
        token = page.get("NextToken")
        if not token:
            return found


def secret_value(run, secret_id):
    try:
        value = run.aws.secrets.get_secret_value(SecretId=secret_id)
    except Exception as error:
        _ignore(error, "ResourceNotFoundException")
        return None
    return {key: value[key] for key in ("SecretString", "SecretBinary") if key in value}


def copy_secrets(run, source, target, overwrite=False):
    """Copy each openclaw/user/<source>/<key> to openclaw/user/<target>/<key>. Returns (copied, unchanged).

    A tag whose value is the source namespace (the api-keys skill's openclaw:user) names the
    target namespace on the copy.
    """
    copied = unchanged = 0
    for secret in user_secrets(run, source):
        name = f"openclaw/user/{target}/" + secret["Name"][len(f"openclaw/user/{source}/"):]
        value = secret_value(run, secret["ARN"])
        if value is None:
            continue  # deleted meanwhile
        existing = secret_value(run, name)
        if existing == value:
            unchanged += 1
            continue
        if existing is not None and not overwrite:
            raise Refused("a secret under the new namespace already has another value; reconcile")
        if run.apply:
            if existing is not None:
                run.aws.secrets.put_secret_value(SecretId=name, **value)
            else:
                detail = run.aws.secrets.describe_secret(SecretId=secret["ARN"])
                extra = {key: detail[key] for key in ("KmsKeyId", "Description") if detail.get(key)}
                tags = [{"Key": tag["Key"], "Value": target if tag.get("Value") == source else tag.get("Value", "")}
                        for tag in detail.get("Tags") or []]
                run.aws.secrets.create_secret(Name=name, **value, **extra, **({"Tags": tags} if tags else {}))
        copied += 1
    return copied, unchanged


def delete_secrets(run, namespace, force):
    found = user_secrets(run, namespace, include_pending=True)
    targets = found if force else [s for s in found if not s.get("DeletedDate")]
    if run.apply:
        options = {"ForceDeleteWithoutRecovery": True} if force else {"RecoveryWindowInDays": RECOVERY_WINDOW_DAYS}
        for secret in targets:
            try:
                run.aws.secrets.delete_secret(SecretId=secret["ARN"], **options)
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
    return len(targets)


def delete_login(run, namespace, force):
    """Delete the Cognito user of namespace and its password secret (bridge/cognito-token.js)."""
    if not run.apply:
        return
    username = run.actor if namespace == runtime_routing.legacy_namespace(run.actor) else namespace
    options = {"ForceDeleteWithoutRecovery": True} if force else {"RecoveryWindowInDays": RECOVERY_WINDOW_DAYS}
    try:
        run.aws.secrets.delete_secret(SecretId=f"openclaw/cognito-user/{run.deployment.user_pool}/{namespace}",
                                      **options)
    except Exception as error:
        # InvalidRequestException: the secret is already scheduled for deletion.
        _ignore(error, "ResourceNotFoundException", "InvalidRequestException")
    try:
        run.aws.cognito.admin_delete_user(UserPoolId=run.deployment.user_pool, Username=username)
    except Exception as error:
        _ignore(error, "UserNotFoundException")


def own_schedules(run, namespace):
    """Names of the schedules of namespace, with the owner check for the collision-prone old name form."""
    scheduler, group, names = run.aws.scheduler, run.deployment.schedule_group, set()
    for prefix, check in schedule_prefixes(namespace):
        token = None
        while True:
            page = scheduler.list_schedules(GroupName=group, NamePrefix=prefix,
                                            **({"NextToken": token} if token else {}))
            for schedule in page.get("Schedules", []):
                name = schedule["Name"]
                if name.startswith(prefix) and (not check or schedule_names_user(run, name)):
                    names.add(name)
            token = page.get("NextToken")
            if not token:
                break
    return names


def get_schedule(run, name):
    try:
        return run.aws.scheduler.get_schedule(Name=name, GroupName=run.deployment.schedule_group)
    except Exception as error:
        _ignore(error, "ResourceNotFoundException")
        return None


def schedule_names_user(run, name):
    detail = get_schedule(run, name) or {}
    try:
        payload = json.loads(detail.get("Target", {}).get("Input") or "{}")
    except ValueError:
        return False
    return payload.get("userId") == run.user_id or payload.get("actorId") == run.actor


def spent(detail, now):
    """True for a one-time at() schedule whose time has passed: it never runs again."""
    match = ONE_TIME_RE.fullmatch(detail.get("ScheduleExpression") or "")
    if not match:
        return False
    moment = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    try:
        return moment.replace(tzinfo=ZoneInfo(detail.get("ScheduleExpressionTimezone") or "UTC")) < now
    except (ValueError, ZoneInfoNotFoundError):
        # An unknown time zone: passed only if it has passed in every zone (UTC-12 is the last).
        return moment.replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=12) < now


def move_schedules(run, source, target, states_field, known_states=None):
    """Recreate each schedule of source under target's name, then delete the source one.

    Returns (moved, kept disabled). The state each schedule gets is, in order: the one this
    pass recorded before an interruption (states_field), known_states (the original states,
    for a rollback), then its current state. A one-time schedule whose time has passed is
    created disabled and stays so: re-enabling it could run it again.
    """
    scheduler, group = run.aws.scheduler, run.deployment.schedule_group
    sources = {}
    for name in own_schedules(run, source):
        schedule_id = name.rsplit("-", 1)[-1]
        if SCHEDULE_ID_RE.fullmatch(schedule_id):
            sources[schedule_id] = name
    states = {**(known_states or {}), **(run.migration.get(states_field) or {})}
    details = {}
    for schedule_id, name in sorted(sources.items()):
        details[schedule_id] = get_schedule(run, name)
        if details[schedule_id] and schedule_id not in states:
            states[schedule_id] = details[schedule_id].get("State", "ENABLED")
    if not run.apply:
        return len(sources), 0
    if states != (run.migration.get(states_field) or {}):
        set_migration_fields(run, **{states_field: states})  # before any source schedule is deleted
    cron = {item["SK"][len("CRON#"):]: item for item in _query(run.table, f"USER#{run.user_id}")
            if item["SK"].startswith("CRON#")}
    for schedule_id, name in sorted(sources.items()):
        detail, new_name = details[schedule_id], build_schedule_name(target, schedule_id)
        if detail is None:
            continue  # deleted meanwhile
        if get_schedule(run, new_name) is None:
            scheduler.create_schedule(Name=new_name, GroupName=group, State="DISABLED",
                                      **{k: detail[k] for k in SCHEDULE_FIELDS if k in detail})
        record_item = cron.get(schedule_id)
        if record_item and record_item.get("ebName") != new_name:
            run.table.update_item(
                Key={"PK": f"USER#{run.user_id}", "SK": f"CRON#{schedule_id}"},
                UpdateExpression="SET #ebName = :new", ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#ebName": "ebName"}, ExpressionAttributeValues={":new": new_name})
        try:
            scheduler.delete_schedule(Name=name, GroupName=group)
        except Exception as error:
            _ignore(error, "ResourceNotFoundException")
    kept_disabled = 0
    for schedule_id, state in sorted(states.items()):
        new_name = build_schedule_name(target, schedule_id)
        detail = get_schedule(run, new_name)
        if not detail:
            continue
        if spent(detail, run.now()):
            state, kept_disabled = "DISABLED", kept_disabled + 1
        if detail.get("State") != state:
            scheduler.update_schedule(Name=new_name, GroupName=group, State=state,
                                      **{k: detail[k] for k in SCHEDULE_FIELDS if k in detail})
    return len(sources), kept_disabled


# ---------------------------------------------------------------------------
# Migrate steps
# ---------------------------------------------------------------------------

def step_plan(run):
    found, _ = listing(run, run.old)
    counts = {"objects": len(found), "bytes": _size(found), "secrets": len(user_secrets(run, run.old)),
              "schedules": len(own_schedules(run, run.old))}
    work = (f"{_count(counts['objects'], 'object')} ({counts['bytes']} bytes), "
            f"{_count(counts['secrets'], 'secret')}, {_count(counts['schedules'], 'schedule')}")
    if too_large(found):
        reason = (f"{_count(len(too_large(found)), 'object')} under the old prefixes is larger than 5 GiB, which "
                  "one CopyObject call cannot copy; copy it under the new namespace with a multipart copy first")
        if run.apply:
            raise Refused(f"plan: {reason}. Nothing was changed")
        run.blocked = reason
        run.say("plan", f"--apply would stop here: {reason}")
        return
    if not run.apply:
        run.say("plan", f"would allocate a new namespace; the old one holds {work}")
        return
    namespace = new_namespace()
    run.table.put_item(Item={"PK": f"NS#{namespace}", "SK": "OWNER", "userId": run.user_id,
                             "createdAt": iso(run.now()), "source": "cutover"},
                       ConditionExpression="attribute_not_exists(PK)")
    item = {**run.migration_key, "state": "PLANNED", "from": run.old, "to": namespace,
            "plannedAt": iso(run.now()), "counts": counts}
    try:
        run.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK) OR #state IN (:rolled, :cleaned)",
                           ExpressionAttributeNames={"#state": "state"},
                           ExpressionAttributeValues={":rolled": "ROLLED_BACK", ":cleaned": "CLEANED"})
    except Exception as error:
        try:
            run.table.delete_item(Key={"PK": f"NS#{namespace}", "SK": "OWNER"}, ConditionExpression="userId = :user",
                                  ExpressionAttributeValues={":user": run.user_id})
        except Exception:  # noqa: BLE001 -- the record holds nothing; the original error matters
            pass
        _ignore(error, "ConditionalCheckFailedException")
        raise Failed("another run planned this user's migration meanwhile; re-run") from None
    run.migration, run.new = item, namespace
    run.say("plan", f"allocated the new namespace; the old one holds {work}")


def step_freeze(run):
    status = run.session.get("routeStatus")
    if status == "MIGRATING" and run.migration and idle(run.session):
        record(run, "FROZEN", frozenAt=iso(run.now()))
        run.say("freeze", "the route was already frozen")
        return
    if status == "MIGRATING" and run.migration:
        raise Refused(f"freeze: {held_reason(run.session)}")
    if status != "ACTIVE":
        raise Refused(f"the route is {status}; finish or roll back that operation first")
    if not idle(run.session):
        if run.apply:
            raise Refused(f"freeze: {held_reason(run.session)}. Nothing was changed")
        run.blocked = held_reason(run.session)
        run.say("freeze", f"--apply would stop here: {run.blocked}")
        return
    route_of(run)
    if run.apply:
        set_route(run, "SET #routeStatus = :migrating", ["#routeStatus = :active"],
                  {":migrating": "MIGRATING", ":active": "ACTIVE"},
                  "freeze: the route changed or took an admission while being frozen; re-run")
        record(run, "FROZEN", frozenAt=iso(run.now()))
    run.say("freeze", run.did("set the route to MIGRATING (the Router sends a maintenance reply)",
                              "set the route to MIGRATING"))


def step_stop(run):
    route_of(run)
    minutes = run.quiesce_seconds // 60
    if not run.apply:
        run.say("stop", f"would stop the AgentCore session and wait until nothing changes under the old prefixes "
                        f"for {minutes} minutes")
        return
    stop_session(run)
    source = quiesce(run, run.old)
    record(run, "STOPPED", stoppedAt=iso(run.now()), sourceDigest=source)
    run.say("stop", f"stopped the AgentCore session; nothing changed under the old prefixes for {minutes} minutes")


def require_current(run, namespace, step):
    if run.current != namespace:
        raise Refused(f"{step}: the route's namespace is not the one this step expects; reconcile")


def step_copy(run):
    require_current(run, run.old, "copy")
    found, _ = listing(run, run.old)
    if not run.new:  # a dry run that has not planned yet
        run.say("copy", f"would copy {_count(len(found), 'object')} ({_size(found)} bytes); older versions stay")
        return
    current = digest(found)
    # Objects already under the new prefix were copied from this listing only if it did not change.
    changed = "sourceDigest" in run.migration and current != run.migration["sourceDigest"]
    keys = sync(run, run.old, run.new, found, full=current != run.migration.get("sourceDigest"))
    record(run, "COPIED", copiedAt=iso(run.now()), copiedDigest=current)
    note = "; the listing changed after the session stopped, so every object was copied again" if changed else ""
    run.say("copy", f"{run.did('copy', 'copied')} {_count(len(keys), 'object')} ({_size(found, keys)} bytes) of "
                    f"{len(found)}{note}; older versions stay")


def step_secrets(run):
    require_current(run, run.old, "secrets")
    if not run.apply:
        run.say("secrets", f"would copy {_count(len(user_secrets(run, run.old)), 'secret')}")
        return
    copied, unchanged = copy_secrets(run, run.old, run.new)
    record(run, "SECRETS")
    run.say("secrets", f"copied {_count(copied, 'secret')} ({unchanged} already copied)")


def step_schedules(run):
    require_current(run, run.old, "schedules")
    moved, disabled = move_schedules(run, run.old, run.new, "scheduleStates")
    if not run.apply:
        run.say("schedules", f"would move {_count(moved, 'schedule')}")
        return
    record(run, "SCHEDULES")
    kept = f"; {_count(disabled, 'one-time schedule')} whose time has passed stays disabled" if disabled else ""
    run.say("schedules", f"moved {_count(moved, 'schedule')} to the new names{kept}")


def step_flip(run):
    if not run.apply:
        run.say("flip", "would store the new namespace on the route and bump its generation")
        return
    if run.current != run.new:
        require_current(run, run.old, "flip")
        if run.session.get("routeStatus") != "MIGRATING":
            raise Refused("flip: the route is not MIGRATING; reconcile")
        set_route(run, "SET #namespace = :new, #generation = #generation + :one", ["#routeStatus = :migrating"],
                  {":new": run.new, ":one": 1, ":migrating": "MIGRATING"},
                  "flip: the route changed or took an admission; re-run")
    record(run, "FLIPPED", flippedAt=iso(run.now()))
    run.say("flip", "stored the new namespace on the route and bumped its generation")


def step_verify(run):
    if not run.apply:
        run.say("verify", "would copy again what changed under the old prefixes and check the secrets and schedules")
        return
    require_current(run, run.new, "verify")
    found, _ = listing(run, run.old)
    current = digest(found)
    late = current != run.migration.get("copiedDigest")
    keys = sync(run, run.old, run.new, found, full=late)
    after, checked = listing(run, run.old)
    if digest(after) != current:
        raise Failed("objects are still changing under the old prefixes; check that the session stopped, then re-run")
    if checked is None:
        raise Failed("S3 sent no Date header, so the cut-over time cannot be recorded; re-run")
    copied_secrets, _ = copy_secrets(run, run.old, run.new, overwrite=True)
    left = own_schedules(run, run.old)
    if left:
        raise Failed(f"{_count(len(left), 'schedule')} still has its old name; re-run")
    # checkedAt (S3's clock): anything written under the new prefixes from then on is the user's,
    # and anything written under the old ones was written after the cut-over.
    record(run, "VERIFIED", verifiedAt=iso(run.now()), checkedAt=iso(checked), lateObjects=len(keys) if late else 0)
    notes = []
    if late:
        notes.append(f"the listing changed after the copy, so {_count(len(keys), 'object')} were copied again")
    elif keys:
        notes.append(f"copied {_count(len(keys), 'missing object')}")
    if copied_secrets:
        notes.append(f"copied {_count(copied_secrets, 'secret')} again")
    run.say("verify", "copies, secrets and schedules are in place" + "".join(f"; {note}" for note in notes))


def step_unfreeze(run):
    if not run.apply:
        run.say("unfreeze", "would set the route back to ACTIVE under the new namespace")
        return
    require_current(run, run.new, "unfreeze")
    if run.session.get("routeStatus") == "MIGRATING":
        set_route(run, "SET #routeStatus = :active", ["#routeStatus = :migrating"],
                  {":active": "ACTIVE", ":migrating": "MIGRATING"}, "unfreeze: the route changed; re-run")
    elif run.session.get("routeStatus") != "ACTIVE":
        raise Refused("unfreeze: the route is neither MIGRATING nor ACTIVE; reconcile")
    record(run, "MOVED", movedAt=iso(run.now()))
    run.say("unfreeze", "the route is ACTIVE under the new namespace")


MIGRATE_STEPS = (("PLANNED", step_plan), ("FROZEN", step_freeze), ("STOPPED", step_stop), ("COPIED", step_copy),
                 ("SECRETS", step_secrets), ("SCHEDULES", step_schedules), ("FLIPPED", step_flip),
                 ("VERIFIED", step_verify), ("MOVED", step_unfreeze))


def hold_end(run):
    moved = parse_iso(run.migration.get("movedAt"))
    return moved + datetime.timedelta(days=run.hold_days) if moved else None


def migrate(run):
    if run.migration.get("rollback") or run.migration.get("rollbackStartedAt"):
        raise Refused("a rollback of this migration has started; run --rollback again to finish it")
    if run.session.get("endpointName") not in run.endpoints and not reached(run, "MOVED"):
        # A route keeps the endpoint it enrolled on (docs/runtime-v2.md), and a bridge image from
        # before stored namespaces derives the namespace from the actor whatever the route says.
        raise Refused(f"the route uses endpoint {run.session.get('endpointName')}, which --endpoint does not name. "
                      "Name only endpoints whose bridge image reads the namespace from the route")
    if not run.migration and not idle(run.session):
        # Refuse before allocating anything; the freeze would refuse anyway.
        if run.apply:
            raise Refused(f"freeze: {held_reason(run.session)}. Nothing was changed")
        run.blocked = held_reason(run.session)
    for state, step in MIGRATE_STEPS:
        if reached(run, state):
            continue
        step(run)
        if run.blocked:
            return
    if run.apply or reached(run, "MOVED"):
        end = hold_end(run)
        run.say("done", f"{run.user_id} uses its new namespace. The old data is kept: --rollback restores it, and "
                        f"--cleanup deletes it from {iso(end) if end else 'the end of the hold'}")


# ---------------------------------------------------------------------------
# Rollback and cleanup
# ---------------------------------------------------------------------------

def rollback_done(run, step):
    return run.migration.get("rollback") in ROLLBACK_STEPS and \
        ROLLBACK_STEPS.index(run.migration["rollback"]) >= ROLLBACK_STEPS.index(step)


def rollback(run):
    if not run.migration:
        raise Refused("there is no migration to roll back")
    flipped = run.current == run.new
    if not run.apply:
        run.say("rollback", "would " + ("freeze and stop the route, copy back what was written under the new "
                                        "namespace since the cut-over, move the schedules back, flip back and "
                                        "unfreeze, then delete the new copies"
                                        if flipped else "move the schedules back, unfreeze and delete the new copies"))
        return
    status = run.session.get("routeStatus")
    if flipped and not rollback_done(run, "freeze"):
        # Checked before anything is recorded, so a refused rollback leaves the migration as it was.
        if status == "ACTIVE" and not idle(run.session):
            raise Refused(f"rollback freeze: {held_reason(run.session)}. Nothing was changed")
        if status not in ("ACTIVE", "MIGRATING"):
            raise Refused("rollback freeze: the route is neither ACTIVE nor MIGRATING; reconcile")
    if not run.migration.get("rollbackStartedAt"):
        set_migration_fields(run, rollbackStartedAt=iso(run.now()))

    def mark(step, text="done"):
        set_migration_fields(run, rollback=step)
        run.say("rollback", f"{step}: {text}")

    if flipped and not rollback_done(run, "freeze"):
        if status == "ACTIVE":
            set_route(run, "SET #routeStatus = :migrating", ["#routeStatus = :active"],
                      {":migrating": "MIGRATING", ":active": "ACTIVE"}, "rollback freeze: the route changed; re-run")
        mark("freeze", "the route is MIGRATING")
    if flipped and not rollback_done(run, "stop"):
        stop_session(run)
        quiesce(run, run.new)
        mark("stop", "stopped the AgentCore session")
    if flipped and not rollback_done(run, "copy back"):
        # Only the user writes under the new prefixes after the verify step's S3 time.
        checked = parse_iso(run.migration.get("checkedAt"))
        keys = []
        if checked:
            found, _ = listing(run, run.new)
            keys = sorted(key for key, entry in found.items() if entry[2] and entry[2] >= checked)
            copy_objects(run, run.new, run.old, found, keys)
        mark("copy back", f"copied back {_count(len(keys), 'object')} written under the new namespace")
    if flipped and not rollback_done(run, "secrets back"):
        copied, _ = copy_secrets(run, run.new, run.old, overwrite=True)
        mark("secrets back", f"copied back {_count(copied, 'secret')}")
    if not rollback_done(run, "schedules back"):
        moved, _ = move_schedules(run, run.new, run.old, "rollbackScheduleStates", run.migration.get("scheduleStates"))
        mark("schedules back", f"moved {_count(moved, 'schedule')} back")
    if not rollback_done(run, "flip back"):
        if run.current == run.new:
            set_route(run, "SET #namespace = :old, #generation = #generation + :one", ["#routeStatus = :migrating"],
                      {":old": run.old, ":one": 1, ":migrating": "MIGRATING"},
                      "rollback flip: the route changed; re-run")
        mark("flip back", "the route stores its old namespace")
    if not rollback_done(run, "unfreeze"):
        require_current(run, run.old, "rollback unfreeze")
        if run.session.get("routeStatus") == "MIGRATING":
            set_route(run, "SET #routeStatus = :active", ["#routeStatus = :migrating"],
                      {":active": "ACTIVE", ":migrating": "MIGRATING"}, "rollback unfreeze: the route changed; re-run")
        mark("unfreeze", "the route is ACTIVE under its old namespace")
    if not rollback_done(run, "remove new"):
        if run.new_confirmed:
            removed = delete_tree(run, run.new)
            delete_secrets(run, run.new, force=True)
            delete_login(run, run.new, force=True)
            delete_owner(run, run.new)
            mark("remove new", f"deleted {_count(removed, 'object version')}, the secrets, the Cognito user and "
                               "the NS# owner record of the new namespace")
        else:
            mark("remove new", "an earlier run already deleted the new namespace's data")
    try:
        run.table.update_item(Key=run.migration_key, UpdateExpression="SET #state = :rolled, #at = :now",
                              ConditionExpression="#state = :state",
                              ExpressionAttributeNames={"#state": "state", "#at": "rolledBackAt"},
                              ExpressionAttributeValues={":rolled": "ROLLED_BACK", ":now": iso(run.now()),
                                                         ":state": run.migration["state"]})
    except Exception as error:
        _ignore(error, "ConditionalCheckFailedException")
        raise Failed("the NS_MIGRATION record changed during the run; re-run") from None
    run.say("rollback", f"{run.user_id} uses its old namespace again")


def cleanup(run):
    if not run.migration or run.migration.get("state") != "MOVED":
        raise Refused("cleanup needs a migration that finished (state MOVED)")
    if run.migration.get("rollback") or run.migration.get("rollbackStartedAt"):
        raise Refused("a rollback of this migration has started; run --rollback again to finish it")
    require_current(run, run.new, "cleanup")
    end, checked = hold_end(run), parse_iso(run.migration.get("checkedAt"))
    if not end or not checked:
        raise Refused("the NS_MIGRATION record has no valid movedAt or checkedAt; reconcile")
    if run.now() < end:
        raise Refused(f"the hold lasts until {iso(end)}; until then --rollback can still restore the old data")
    if not run.old_confirmed:
        # An earlier --cleanup run deleted the old data and then NS#<old>; only the record is left.
        if run.apply:
            finish_cleanup(run)
        run.say("cleanup", run.did("record the finished cleanup", "recorded the finished cleanup"))
        return
    found, _ = listing(run, run.old)
    written = [key for key, entry in found.items() if entry[2] and entry[2] >= checked]
    if written:
        raise Refused(f"{_count(len(written), 'object')} under the old prefixes was written after the cut-over was "
                      "verified, so something still wrote there. Compare them with the new namespace, copy what is "
                      "needed, delete them from the old prefixes, then re-run")
    versions = delete_tree(run, run.old)
    removed = delete_secrets(run, run.old, force=False)
    streams, token = [], None
    while True:
        try:
            page = run.aws.logs.describe_log_streams(logGroupName=CONTAINER_LOG_GROUP,
                                                     logStreamNamePrefix=f"{run.old}-",
                                                     **({"nextToken": token} if token else {}))
        except Exception as error:
            _ignore(error, "ResourceNotFoundException")
            break
        streams += [s["logStreamName"] for s in page.get("logStreams", [])
                    if s["logStreamName"].startswith(f"{run.old}-")]
        token = page.get("nextToken")
        if not token:
            break
    if run.apply:
        delete_login(run, run.old, force=False)
        for name in streams:
            try:
                run.aws.logs.delete_log_stream(logGroupName=CONTAINER_LOG_GROUP, logStreamName=name)
            except Exception as error:
                _ignore(error, "ResourceNotFoundException")
        delete_owner(run, run.old)
        finish_cleanup(run)
    run.say("cleanup", f"{run.did('delete', 'deleted')} {_count(versions, 'object version')} under the old prefixes, "
                       f"{_count(removed, 'secret')} ({RECOVERY_WINDOW_DAYS}-day recovery window), the old Cognito "
                       f"user and its secret, {_count(len(streams), 'container log stream')} and the old NS# owner "
                       "record")


def delete_owner(run, namespace):
    """Delete NS#<namespace>/OWNER while it names this user."""
    try:
        run.table.delete_item(Key={"PK": f"NS#{namespace}", "SK": "OWNER"}, ConditionExpression="userId = :user",
                              ExpressionAttributeValues={":user": run.user_id})
    except Exception as error:
        _ignore(error, "ConditionalCheckFailedException")


def finish_cleanup(run):
    try:
        run.table.update_item(Key=run.migration_key, UpdateExpression="SET #state = :cleaned, #at = :now",
                              ConditionExpression="#state = :moved",
                              ExpressionAttributeNames={"#state": "state", "#at": "cleanedAt"},
                              ExpressionAttributeValues={":cleaned": "CLEANED", ":now": iso(run.now()),
                                                         ":moved": "MOVED"})
    except Exception as error:
        _ignore(error, "ConditionalCheckFailedException")
        raise Failed("the NS_MIGRATION record changed during the run; re-run") from None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="namespace_cutover.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Move one user's storage to a new opaque namespace. Nothing is changed unless --apply is given.",
        epilog="exit status: 0 done or dry run, 1 a step failed (re-run to continue), 2 bad arguments, "
               "3 refused\nsee docs/operations.md, 'Namespace migration'")
    parser.add_argument("--user-id", required=True, help="internal user ID, user_<16 hex>")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rollback", action="store_true", help="return the user to its old namespace")
    mode.add_argument("--cleanup", action="store_true", help="delete the old namespace's data after the hold")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is changed")
    parser.add_argument("--endpoint", action="append", default=[], metavar="NAME",
                        help="required to migrate, repeatable: an AgentCore Runtime endpoint whose bridge image "
                             "reads the namespace from the route; a user on any other endpoint is refused")
    parser.add_argument("--quiesce-minutes", type=int, default=10,
                        help="minutes the old prefixes must stay unchanged before copying (default 10)")
    parser.add_argument("--hold-days", type=int, default=14, help="days to keep the old data (default 14)")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION, AWS_DEFAULT_REGION, then cdk.json)")
    parser.add_argument("--env-suffix", help="deployment environment suffix (default: OPENCLAW_ENV_SUFFIX)")
    return parser


def main(argv=None, aws=None, out=None, now=None, monotonic=time.monotonic, sleep=time.sleep):
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if not USER_ID_RE.fullmatch(args.user_id):
        parser.error("--user-id must be user_<16 hex>")
    if args.quiesce_minutes < 0 or args.hold_days < 0:
        parser.error("--quiesce-minutes and --hold-days must not be negative")
    if not (args.rollback or args.cleanup or args.endpoint):
        parser.error("--endpoint is required: name the endpoints whose bridge image reads the namespace from the route")
    raw_suffix = args.env_suffix if args.env_suffix is not None else os.environ.get("OPENCLAW_ENV_SUFFIX", "")
    try:
        suffix = env_suffix({"OPENCLAW_ENV_SUFFIX": raw_suffix})
    except ValueError as error:
        parser.error(str(error))
    region = _region(args.region)
    if not region:
        parser.error("cannot determine the AWS region; pass --region")
    aws = aws or Aws(region)
    action = "rollback" if args.rollback else "cleanup" if args.cleanup else "migrate"
    mode = "APPLY" if args.apply else "DRY RUN, nothing is changed (add --apply to write)"
    print(f"namespace_cutover {action}: {mode}; region {region}, environment suffix {suffix or '(none)'}", file=out)
    try:
        deployment = resolve_deployment(aws.cfn, suffix)
        run = Run(aws, deployment, aws.table(deployment.identity_table), args.user_id, args.apply, out,
                  now or (lambda: datetime.datetime.now(datetime.timezone.utc)), monotonic, sleep,
                  args.quiesce_minutes * 60, args.hold_days, tuple(args.endpoint))
        load(run)
        state = run.migration.get("state", "none")
        run.say("load", f"{run.user_id}: migration state {state}"
                        + (f", rollback step {run.migration['rollback']}" if run.migration.get("rollback") else ""))
        {"migrate": migrate, "rollback": rollback, "cleanup": cleanup}[action](run)
        if run.blocked:
            print(f"--apply would be refused: {run.blocked}.", file=out)
            return EXIT_REFUSED
        return EXIT_OK
    except Refused as error:
        print(f"REFUSED: {error}", file=out)
        return EXIT_REFUSED
    except Exception as error:
        # Error codes and this tool's own messages only: AWS messages can name resources.
        detail = _code(error) or (str(error) if isinstance(error, Failed) else type(error).__name__)
        print(f"FAILED: {detail}. Re-run the same command to continue.", file=out)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
