"""Tests for scripts/namespace_cutover.py against the stateful fakes in tests/_namespace_fakes.py.

No AWS calls. A run that fails part-way is re-run against what it left, and must end in the
same state as an uninterrupted run, without copying an object twice. All IDs are synthetic.
"""

import copy
import datetime
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from tests._namespace_fakes import (
    CRON_LAMBDA, POOL, SCHEDULER_ROLE, Aws, client_error, enrol, schedule_payload,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "lambda" / "shared" / "python"))
import delete_user_data  # noqa: E402
import namespace_cutover as cutover  # noqa: E402
import runtime_routing  # noqa: E402

ALLOCATE = cutover.new_namespace


def derived(actor):
    return f"user_{hashlib.sha256(actor.encode()).hexdigest()[:16]}"


RUNTIME, OTHER = "telegram:100000001", "telegram:1000000012"
USER, OTHER_USER = derived(RUNTIME), derived(OTHER)
LEGACY, OTHER_NS = "telegram_100000001", "telegram_1000000012"  # the other one starts with ours
NEW, NEWER = "ns_" + "b" * 26, "ns_" + "c" * 26
SCHEDULES = {"a1b2c3d4": ("ENABLED", "cron(0 9 * * ? *)"), "e5f6a7b8": ("DISABLED", "rate(1 day)"),
             "0badf00d": ("ENABLED", "at(2026-01-01T09:00:00)"),  # one-time, passed
             "c0ffee00": ("ENABLED", "at(2027-01-01T09:00:00)")}  # one-time, still to come
TREE = {".openclaw/state.db": "state-2", "_uploads/img_1.jpg": "jpeg", "notes/todo.md": "todo"}
DAY = 86400


@pytest.fixture(autouse=True)
def allocate(monkeypatch):
    """The namespaces the plan step allocates, in order (default: NEW every time)."""
    names = []
    monkeypatch.setattr(cutover, "new_namespace", lambda: names.pop(0) if names else NEW)
    return names


def tags(namespace):
    return [{"Key": "openclaw:user", "Value": namespace}, {"Key": "openclaw:managed", "Value": "true"}]


def world(backfilled=True):
    """Our user (backfilled to its legacy namespace) and another user whose namespace starts with ours."""
    aws = Aws()
    enrol(aws, RUNTIME, USER, namespace=LEGACY if backfilled else None)
    enrol(aws, OTHER, OTHER_USER, namespace=OTHER_NS)
    for ns in (LEGACY, OTHER_NS):
        aws.s3.put(f"{ns}/.openclaw/state.db", "state-1")
        aws.s3.put(f"{ns}/.openclaw/state.db", "state-2")
        aws.s3.put(f"{ns}/_uploads/img_1.jpg", "jpeg")
        aws.s3.put(f"{ns}/notes/todo.md", "todo")
        aws.s3.put(f"{ns}/notes/gone.md", "gone")
        aws.s3.remove(f"{ns}/notes/gone.md")
        aws.s3.put(f"_system/{ns}/AGENTS.md", "agents")
        aws.secrets.add(f"openclaw/user/{ns}/OPENAI_API_KEY", f"sk-{ns}", tags=tags(ns))
        aws.secrets.add(f"openclaw/cognito-user/{POOL}/{ns}", "cognito-password")
        aws.logs.streams.add(f"{ns}-1730000000000")
    aws.secrets.add(f"openclaw/user/{LEGACY}/GITHUB_TOKEN", "ghp-synthetic", tags=tags(LEGACY))
    aws.secrets.add(f"openclaw/user/{LEGACY}/OLD_KEY", "deleted-by-the-user", tags=tags(LEGACY), pending=True)
    aws.logs.streams.add(f"{USER}-1730000200000")
    aws.cognito.users |= {RUNTIME, OTHER}
    for sid, (state, expression) in SCHEDULES.items():
        aws.scheduler.add(f"openclaw-{LEGACY}-{sid}", schedule_payload(USER, RUNTIME, sid), state, expression)
        aws.identity.put(PK=f"USER#{USER}", SK=f"CRON#{sid}", scheduleId=sid, ebName=f"openclaw-{LEGACY}-{sid}")
    aws.scheduler.add(f"openclaw-{OTHER_NS}-aaaa0000", schedule_payload(OTHER_USER, OTHER, "aaaa0000"))
    return aws


def run(aws, *args, quiesce=0, user=USER, endpoints=("release_1",)):
    out = io.StringIO()
    code = cutover.main(["--region", "us-west-2", "--env-suffix", "", "--user-id", user,
                         "--quiesce-minutes", str(quiesce), *[f"--endpoint={name}" for name in endpoints], *args],
                        aws=aws, out=out, now=aws.clock.now, monotonic=aws.clock.monotonic, sleep=aws.clock.sleep)
    return code, out.getvalue()


def session(aws):
    return aws.identity.get(f"USER#{USER}", "SESSION")


def migration(aws):
    return aws.identity.get(f"USER#{USER}", "NS_MIGRATION")


def theirs(aws):
    """The other user's data, which every run must leave alone."""
    return copy.deepcopy((
        {k: v for k, v in aws.identity.items.items() if OTHER_USER in k[0] or OTHER in k[0] or OTHER_NS in k[0]},
        {k: [(v["Body"], v["Size"], v["marker"]) for v in versions]
         for k, versions in aws.s3.versions.items() if OTHER_NS in k},
        {k: v for k, v in aws.scheduler.schedules.items() if OTHER_NS in k},
        {k: v for k, v in aws.secrets.secrets.items() if OTHER_NS in k},
        OTHER in aws.cognito.users, {s for s in aws.logs.streams if OTHER_NS in s}))


def legacy_versions(aws):
    return copy.deepcopy({k: v for k, v in aws.s3.versions.items()
                          if k.startswith((f"{LEGACY}/", f"_system/{LEGACY}/"))})


def summary(aws):
    """The state with timestamps, digests, ETags and version IDs left out."""
    volatile = {"sourceDigest", "copiedDigest"}
    items = {k: {f: v for f, v in item.items() if not f.endswith("At") and f not in volatile}
             for k, item in aws.identity.items.items()}
    objects = {k: [(v["Body"], v["marker"]) for v in versions] for k, versions in aws.s3.versions.items()}
    schedules = {n: (s["State"], s["ScheduleExpression"], s["ScheduleExpressionTimezone"], s["Target"],
                     s.get("Description")) for n, s in aws.scheduler.schedules.items()}
    stored = {n: (s["SecretString"], sorted((t["Key"], t["Value"]) for t in s["Tags"]), bool(s.get("DeletedDate")))
              for n, s in aws.secrets.secrets.items()}
    return items, objects, schedules, stored, sorted(aws.cognito.users), sorted(aws.logs.streams)


def moved(aws, before_legacy, before_theirs):
    """Everything an uninterrupted cut-over to NEW produces."""
    s = session(aws)
    assert (s["namespace"], s["routeStatus"], s["generation"]) == (NEW, "ACTIVE", 2)
    assert aws.identity.get(f"NS#{NEW}", "OWNER")["userId"] == USER
    assert aws.identity.get(f"NS#{LEGACY}", "OWNER")["userId"] == USER  # kept for the hold
    record = migration(aws)
    assert (record["state"], record["from"], record["to"], record["lateObjects"]) == ("MOVED", LEGACY, NEW, 0)
    # Current versions only, each copied once; the old prefixes are left as they were.
    assert aws.s3.tree(f"{NEW}/") == aws.s3.tree(f"{LEGACY}/") == TREE
    assert aws.s3.tree(f"_system/{NEW}/") == {"AGENTS.md": "agents"}
    assert aws.s3.count(f"{NEW}/") == 3 and aws.s3.count(f"_system/{NEW}/") == 1
    assert legacy_versions(aws) == before_legacy
    # Secrets: live ones copied with the tag naming the new namespace; nothing else moves.
    assert {n: aws.secrets.live(n) for n in aws.secrets.secrets if n.startswith(f"openclaw/user/{NEW}/")} == {
        f"openclaw/user/{NEW}/OPENAI_API_KEY": f"sk-{LEGACY}", f"openclaw/user/{NEW}/GITHUB_TOKEN": "ghp-synthetic"}
    assert aws.secrets.secrets[f"openclaw/user/{NEW}/OPENAI_API_KEY"]["Tags"] == tags(NEW)
    assert aws.secrets.live(f"openclaw/user/{LEGACY}/OPENAI_API_KEY") == f"sk-{LEGACY}"
    assert f"openclaw/cognito-user/{POOL}/{NEW}" not in aws.secrets.secrets  # the bridge creates it
    # Schedules: new names, same input (D9), same state; the passed one-time schedule is disabled.
    for sid, (state, expression) in SCHEDULES.items():
        assert f"openclaw-{LEGACY}-{sid}" not in aws.scheduler.schedules
        schedule = aws.scheduler.schedules[f"openclaw-{NEW}-{sid}"]
        assert schedule["Target"] == {"Arn": CRON_LAMBDA, "RoleArn": SCHEDULER_ROLE,
                                      "Input": json.dumps(schedule_payload(USER, RUNTIME, sid))}
        assert (schedule["ScheduleExpression"], schedule["ScheduleExpressionTimezone"]) == (expression, "Europe/Berlin")
        assert schedule["State"] == ("DISABLED" if sid == "0badf00d" else state), sid
        assert aws.identity.get(f"USER#{USER}", f"CRON#{sid}")["ebName"] == f"openclaw-{NEW}-{sid}"
    assert theirs(aws) == before_theirs
    route = runtime_routing.resolve_route(aws.identity, USER)
    assert route.namespace == NEW and route.namespace_stored and route.payload_context()["namespace"] == NEW


# ---------------------------------------------------------------------------
# Migrate
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing_and_lists_every_step():
    aws = world()
    before = aws.state()
    code, out = run(aws)
    assert code == 0, out
    assert aws.log.mutating() == [] and aws.state() == before
    for step in ("plan", "freeze", "stop", "copy", "secrets", "schedules", "flip", "verify", "unfreeze"):
        assert f"[{step}] would " in out, step
    assert "the old one holds 4 objects (" in out and "2 secrets, 4 schedules" in out
    assert "would copy 4 objects" in out and "would move 4 schedules" in out


def test_apply_moves_the_user_and_the_router_resolves_the_new_namespace():
    aws = world()
    before_legacy, before_theirs = legacy_versions(aws), theirs(aws)
    code, out = run(aws, "--apply")
    assert code == 0, out
    moved(aws, before_legacy, before_theirs)
    assert len(aws.s3.copies) == 4
    assert aws.agentcore.stopped == [runtime_routing.route_from_record(
        {**session(aws), "generation": 1, "namespace": LEGACY}, USER).invocation()]
    assert "[done] " + USER + " uses its new namespace" in out
    assert "1 one-time schedule whose time has passed stays disabled" in out
    # The output names the user by internal user ID only.
    for secret in ("100000001", LEGACY, NEW, POOL):
        assert secret not in out, secret
    # A second run has nothing left to do.
    calls = len(aws.log.mutating())
    code, out = run(aws, "--apply")
    assert code == 0 and len(aws.log.mutating()) == calls and "migration state MOVED" in out


def test_the_route_is_migrating_while_the_data_moves():
    aws = world()
    seen = []

    def check():
        with pytest.raises(runtime_routing.RouteMigrating):
            runtime_routing.resolve_route(aws.identity, USER)
        seen.append(session(aws)["namespace"])

    aws.agentcore.on("stop_runtime_session", check)
    aws.scheduler.on("delete_schedule", check)
    code, out = run(aws, "--apply")
    assert code == 0, out
    assert seen == [LEGACY, LEGACY]
    assert runtime_routing.resolve_route(aws.identity, USER).namespace == NEW


@pytest.mark.parametrize("failure", [
    ("agentcore", "stop_runtime_session", "ThrottlingException", 0),
    ("s3", "copy_object", "InternalError", 2),
    ("secrets", "create_secret", "ThrottlingException", 1),
    ("scheduler", "create_schedule", "InternalServerException", 1),
    ("scheduler", "delete_schedule", "InternalServerException", 2),
    ("scheduler", "update_schedule", "InternalServerException", 0),
    ("identity", ("put_item", ("USER#" + USER, "NS_MIGRATION")), "InternalServerError", 0),
    ("identity", ("update_item", ("USER#" + USER, "SESSION")), "InternalServerError", 1),  # the flip
    ("identity", ("update_item", ("USER#" + USER, "SESSION")), "InternalServerError", 2),  # the unfreeze
    ("identity", ("update_item", ("USER#" + USER, "NS_MIGRATION")), "ProvisionedThroughputExceededException", 2),
    ("identity", ("update_item", ("USER#" + USER, "NS_MIGRATION")), "ProvisionedThroughputExceededException", 6),
], ids=lambda failure: f"{failure[1] if isinstance(failure[1], str) else failure[1][0]}-{failure[3]}")
def test_a_rerun_after_a_failure_finishes_like_an_uninterrupted_run(failure):
    expected = world()
    assert run(expected, "--apply")[0] == 0
    aws = world()
    before_legacy, before_theirs = legacy_versions(aws), theirs(aws)
    where, call, code_name, after = failure
    if where == "identity":
        aws.identity.fail_on(call[0], call[1], client_error(code_name), after=after)
    else:
        getattr(aws, where).fail_on(call, client_error(code_name), after=after)
    code, out = run(aws, "--apply")
    assert code == 1 and f"FAILED: {code_name}. Re-run the same command to continue." in out, out
    code, out = run(aws, "--apply")
    assert code == 0, out
    moved(aws, before_legacy, before_theirs)
    assert summary(aws) == summary(expected)
    assert len(aws.s3.copies) == len(expected.s3.copies) == 4  # nothing was copied twice


def test_a_failed_plan_leaves_no_owner_record_behind():
    aws = world()
    aws.identity.fail_on("put_item", (f"USER#{USER}", "NS_MIGRATION"), client_error("InternalServerError"))
    code, out = run(aws, "--apply")
    assert code == 1, out
    assert aws.identity.get(f"NS#{NEW}", "OWNER") is None and migration(aws) is None
    assert session(aws)["routeStatus"] == "ACTIVE"


def test_held_admissions_refuse_the_cutover_before_anything_changes():
    aws = world()
    session(aws).update(activeAdmissions={"token-1"}, inFlight=1)
    before = aws.state()
    code, out = run(aws)
    assert code == 3 and "--apply would be refused: the route holds 1 invocation admission" in out, out
    code, out = run(aws, "--apply")
    assert code == 3 and "REFUSED: freeze: the route holds 1 invocation admission" in out, out
    assert "Nothing was changed" in out
    assert aws.log.mutating() == [] and aws.state() == before
    del session(aws)["activeAdmissions"], session(aws)["inFlight"]
    assert run(aws, "--apply")[0] == 0
    assert session(aws)["namespace"] == NEW


def test_an_admission_taken_while_freezing_refuses_the_freeze():
    aws = world()
    real = aws.identity.update_item

    def admit_first(**kwargs):
        aws.identity.update_item = real
        if kwargs["Key"]["SK"] == "SESSION":
            session(aws).update(activeAdmissions={"token-2"}, inFlight=1)  # a Router admission wins the race
        return real(**kwargs)

    aws.identity.update_item = admit_first
    code, out = run(aws, "--apply")
    assert code == 3 and "REFUSED: freeze: the route changed or took an admission" in out, out
    assert session(aws)["routeStatus"] == "ACTIVE" and migration(aws)["state"] == "PLANNED"


def test_a_write_in_the_quiet_window_restarts_it():
    aws = world()
    started = aws.clock.now()
    aws.agentcore.on("stop_runtime_session",
                     lambda: aws.clock.later(300, lambda: aws.s3.put(f"{LEGACY}/notes/late.md", "late")))
    code, out = run(aws, "--apply", quiesce=10)
    assert code == 0, out
    assert aws.clock.now() - started >= datetime.timedelta(minutes=15)
    assert aws.s3.tree(f"{NEW}/")["notes/late.md"] == "late"
    assert migration(aws)["lateObjects"] == 0


def test_writes_that_do_not_stop_fail_the_stop_step_and_a_rerun_continues():
    aws = world()
    writes = []

    def keep_writing():
        if len(writes) < 20:
            writes.append(1)
            aws.s3.put(f"{LEGACY}/.openclaw/state.db", f"state-{len(writes) + 2}")
            aws.clock.later(120, keep_writing)

    aws.agentcore.on("stop_runtime_session", lambda: aws.clock.later(60, keep_writing))
    code, out = run(aws, "--apply", quiesce=5)
    assert code == 1 and "objects are still being written under the prefix" in out, out
    assert session(aws)["routeStatus"] == "MIGRATING" and migration(aws)["state"] == "FROZEN"
    aws.clock.advance(3600)  # the writer gave up
    code, out = run(aws, "--apply", quiesce=5)
    assert code == 0, out
    assert aws.s3.tree(f"{NEW}/")[".openclaw/state.db"] == f"state-{len(writes) + 2}"


def test_a_write_after_the_copy_is_copied_again_by_verify():
    aws = world()
    aws.secrets.on("create_secret", lambda: aws.s3.put(f"{LEGACY}/notes/todo.md", "todo, edited late"))
    code, out = run(aws, "--apply")
    assert code == 0, out
    assert aws.s3.tree(f"{NEW}/")["notes/todo.md"] == "todo, edited late"
    assert "the listing changed after the copy, so 4 objects were copied again" in out
    assert migration(aws)["lateObjects"] == 4


def test_a_write_during_verify_fails_it_and_a_rerun_continues():
    aws = world()
    aws.secrets.on("create_secret", lambda: aws.s3.put(f"{LEGACY}/notes/todo.md", "edit 1"))
    real, calls = aws.s3.copy_object, []

    def copy_object(**params):
        calls.append(params["Key"])
        result = real(**params)
        if len(calls) == 6:  # the copy step made 4 copies; this is verify copying everything again
            aws.s3.put(f"{LEGACY}/notes/todo.md", "edit 2")
        return result

    aws.s3.copy_object = copy_object
    code, out = run(aws, "--apply")
    assert code == 1 and "objects are still changing under the old prefixes" in out, out
    assert migration(aws)["state"] == "FLIPPED" and session(aws)["routeStatus"] == "MIGRATING"
    code, out = run(aws, "--apply")
    assert code == 0, out
    assert aws.s3.tree(f"{NEW}/")["notes/todo.md"] == "edit 2"


def test_an_object_too_large_for_one_copy_is_refused_before_anything_changes():
    aws = world()
    aws.s3.put(f"{LEGACY}/backups/huge.tar", "huge", size=6 * 1024 ** 3)
    code, out = run(aws)
    assert code == 3 and "--apply would be refused: 1 object under the old prefixes is larger than 5 GiB" in out
    before = aws.state()
    code, out = run(aws, "--apply")
    assert code == 3 and "REFUSED: plan: 1 object" in out and "Nothing was changed" in out
    assert aws.log.mutating() == [] and aws.state() == before


# ---------------------------------------------------------------------------
# What the tool trusts
# ---------------------------------------------------------------------------


def test_a_route_without_a_stored_namespace_needs_the_backfill_first():
    aws = world(backfilled=False)
    code, out = run(aws, "--apply")
    assert code == 3 and "run scripts/namespace_backfill.py first" in out
    assert aws.log.mutating() == []


@pytest.mark.parametrize("tamper", [
    lambda aws: session(aws).update(namespace=OTHER_NS),               # another user's legacy namespace
    lambda aws: session(aws).update(namespace="ns_" + "z" * 26),       # opaque, no NS# record
    lambda aws: (session(aws).update(namespace="ns_" + "z" * 26),
                 aws.identity.put(PK="NS#ns_" + "z" * 26, SK="OWNER", userId=OTHER_USER)),
    lambda aws: aws.identity.items.pop((f"NS#{LEGACY}", "OWNER")),     # the backfill's record is missing
    lambda aws: aws.identity.items.pop((f"ACTOR#{RUNTIME}", "OWNER")),
    lambda aws: aws.identity.get(f"ACTOR#{RUNTIME}", "OWNER").update(userId=OTHER_USER),
    lambda aws: session(aws).update(actorId=OTHER),
], ids=["other-legacy", "opaque-unowned", "opaque-other-owner", "legacy-unowned", "actor-unowned",
        "actor-other-owner", "actor-swapped"])
def test_a_route_the_records_outside_the_partition_do_not_confirm_is_refused(tamper):
    aws = world()
    tamper(aws)
    before = aws.state()
    for args in ((), ("--apply",), ("--rollback", "--apply"), ("--cleanup", "--apply")):
        code, out = run(aws, *args)
        assert code == 3 and "REFUSED" in out, (args, out)
    assert aws.log.mutating() == [] and aws.state() == before


@pytest.mark.parametrize("tamper", [
    {"to": OTHER_NS},                                     # another user's legacy namespace
    {"to": "telegram_999"},                               # not opaque, though its NS# record names us (below)
    {"to": "ns_" + "y" * 26},                             # opaque, owned by another user (below)
    {"to": "ns_" + "x" * 26},                             # opaque, no NS# record
    {"from": OTHER_NS},                                   # not a namespace of this actor
    {"state": "DONE"},
    {"rollback": "rm -rf"},
], ids=["to-other-legacy", "to-not-opaque", "to-other-owner", "to-unowned", "from-other", "state", "rollback-step"])
def test_a_tampered_migration_record_is_refused(tamper):
    aws = world()
    aws.identity.put(PK="NS#ns_" + "y" * 26, SK="OWNER", userId=OTHER_USER)
    aws.identity.put(PK="NS#telegram_999", SK="OWNER", userId=USER)
    aws.identity.put(PK=f"USER#{USER}", SK="NS_MIGRATION", state="FROZEN", **{"from": LEGACY, "to": NEW},
                     plannedAt="2026-11-05T09:00:00Z")
    aws.identity.put(PK=f"NS#{NEW}", SK="OWNER", userId=USER)
    session(aws)["routeStatus"] = "MIGRATING"
    migration(aws).update(tamper)
    before = aws.state()
    for args in ((), ("--apply",), ("--rollback", "--apply"), ("--cleanup", "--apply")):
        code, out = run(aws, *args)
        assert code == 3 and "REFUSED" in out, (args, out)
    assert aws.log.mutating() == [] and aws.state() == before


@pytest.mark.parametrize("args", [
    ["--endpoint", "release_1"], ["--user-id", "user_xyz", "--endpoint", "release_1"],
    ["--user-id", USER, "--rollback", "--cleanup"],
    ["--user-id", USER, "--endpoint", "release_1", "--quiesce-minutes", "-1"],
    ["--user-id", USER, "--endpoint", "release_1", "--env-suffix", "Bad_Suffix"],
    ["--user-id", USER, "--apply"],  # migrating needs --endpoint
])
def test_bad_arguments_exit_2(args):
    with pytest.raises(SystemExit) as raised:
        cutover.main(["--region", "us-west-2", *args], aws=world(), out=io.StringIO())
    assert raised.value.code == 2


def test_a_route_on_an_endpoint_not_named_is_refused():
    aws = world()
    before = aws.state()
    for args in ((), ("--apply",)):
        code, out = run(aws, *args, endpoints=("release_2", "release_3"))
        assert code == 3 and "the route uses endpoint release_1, which --endpoint does not name" in out, out
    assert aws.log.mutating() == [] and aws.state() == before
    # Rollback and cleanup do not depend on the bridge release.
    assert run(aws, "--apply", endpoints=("release_2", "release_1"))[0] == 0
    code, out = run(aws, "--rollback", "--apply", endpoints=())
    assert code == 0, out


def test_new_namespaces_are_opaque_and_random():
    names = {ALLOCATE() for _ in range(200)}
    assert len(names) == 200
    assert all(runtime_routing.OPAQUE_NAMESPACE.fullmatch(name) for name in names)
    assert cutover.build_schedule_name(NEW, "a1b2c3d4") == f"openclaw-{NEW}-a1b2c3d4"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def back(aws, before):
    """The user is on its legacy namespace again and nothing of the new one is left."""
    s = session(aws)
    assert s["namespace"] == LEGACY and s["routeStatus"] == "ACTIVE"
    assert migration(aws)["state"] == "ROLLED_BACK"
    assert aws.identity.get(f"NS#{NEW}", "OWNER") is None
    assert aws.s3.count(f"{NEW}/") == aws.s3.count(f"_system/{NEW}/") == 0
    assert not [n for n in aws.secrets.secrets if f"/{NEW}" in n]
    assert NEW not in aws.cognito.users
    for sid, (state, _) in SCHEDULES.items():
        assert f"openclaw-{NEW}-{sid}" not in aws.scheduler.schedules
        schedule = aws.scheduler.schedules[f"openclaw-{LEGACY}-{sid}"]
        assert schedule["State"] == ("DISABLED" if sid == "0badf00d" else state), sid
        assert schedule["Target"]["Input"] == before.scheduler.schedules[f"openclaw-{LEGACY}-{sid}"]["Target"]["Input"]
        assert aws.identity.get(f"USER#{USER}", f"CRON#{sid}")["ebName"] == f"openclaw-{LEGACY}-{sid}"
    assert runtime_routing.resolve_route(aws.identity, USER).namespace == LEGACY
    assert theirs(aws) == theirs(before)


@pytest.mark.parametrize("stop_at", [
    ("scheduler", "delete_schedule", 1),                               # schedules half moved
    ("identity", ("update_item", ("USER#" + USER, "SESSION")), 1),     # the flip
])
def test_rollback_before_the_flip(stop_at):
    before = world()
    aws = world()
    before_legacy = legacy_versions(aws)
    where, call, after = stop_at
    if where == "identity":
        aws.identity.fail_on(call[0], call[1], client_error("InternalServerError"), after=after)
    else:
        getattr(aws, where).fail_on(call, client_error("InternalServerError"), after=after)
    assert run(aws, "--apply")[0] == 1
    assert session(aws)["namespace"] == LEGACY and session(aws)["routeStatus"] == "MIGRATING"
    code, out = run(aws, "--rollback")
    assert code == 0 and "would move the schedules back, unfreeze and delete the new copies" in out
    code, out = run(aws, "--rollback", "--apply")
    assert code == 0, out
    back(aws, before)
    assert session(aws)["generation"] == 1  # never flipped
    assert legacy_versions(aws) == before_legacy  # nothing was copied back
    assert aws.s3.tree(f"{LEGACY}/") == TREE


def test_rollback_after_the_cutover_keeps_what_the_user_wrote():
    before = world()
    aws = world()
    assert run(aws, "--apply")[0] == 0
    # The user's next turns, under the new namespace (bridge, skills and Cognito token cache).
    aws.s3.put(f"{NEW}/notes/todo.md", "todo, edited after the cut-over")
    aws.s3.put(f"{NEW}/notes/new.md", "written after the cut-over")
    aws.s3.put(f"_system/{NEW}/USER.md", "user profile")
    aws.secrets.secrets[f"openclaw/user/{NEW}/GITHUB_TOKEN"]["SecretString"] = "ghp-rotated"
    aws.secrets.add(f"openclaw/user/{NEW}/NEW_KEY", "new-key", tags=tags(NEW))
    aws.secrets.add(f"openclaw/cognito-user/{POOL}/{NEW}", "cognito-password-2")
    aws.cognito.users.add(NEW)
    code, out = run(aws, "--rollback")
    assert code == 0 and "would freeze and stop the route" in out
    code, out = run(aws, "--rollback", "--apply")
    assert code == 0, out
    back(aws, before)
    assert session(aws)["generation"] == 3  # flipped and flipped back
    assert aws.s3.tree(f"{LEGACY}/") == {**TREE, "notes/todo.md": "todo, edited after the cut-over",
                                         "notes/new.md": "written after the cut-over"}
    assert aws.s3.tree(f"_system/{LEGACY}/") == {"AGENTS.md": "agents", "USER.md": "user profile"}
    assert "copy back: copied back 3 objects written under the new namespace" in out
    assert aws.secrets.live(f"openclaw/user/{LEGACY}/GITHUB_TOKEN") == "ghp-rotated"
    assert aws.secrets.live(f"openclaw/user/{LEGACY}/NEW_KEY") == "new-key"
    assert aws.secrets.secrets[f"openclaw/user/{LEGACY}/NEW_KEY"]["Tags"] == tags(LEGACY)
    assert RUNTIME in aws.cognito.users  # the old Cognito user was never deleted
    assert len(aws.agentcore.stopped) == 2
    # A new migration can start from here.
    assert run(aws, "--apply")[0] == 0
    assert session(aws)["namespace"] == NEW and aws.s3.tree(f"{NEW}/")["notes/new.md"] == "written after the cut-over"


@pytest.mark.parametrize("failure", [
    ("s3", "copy_object", 0), ("secrets", "put_secret_value", 0), ("scheduler", "create_schedule", 2),
    ("s3", "delete_objects", 0), ("cognito", "admin_delete_user", 0),
    ("identity", ("delete_item", (f"NS#{NEW}", "OWNER")), 0),
    ("identity", ("update_item", ("USER#" + USER, "NS_MIGRATION")), 9),
])
def test_an_interrupted_rollback_finishes_when_run_again(failure):
    before = world()
    aws = world()
    assert run(aws, "--apply")[0] == 0
    aws.s3.put(f"{NEW}/notes/new.md", "written after the cut-over")
    aws.secrets.secrets[f"openclaw/user/{NEW}/GITHUB_TOKEN"]["SecretString"] = "ghp-rotated"
    aws.cognito.users.add(NEW)
    where, call, after = failure
    if where == "identity":
        aws.identity.fail_on(call[0], call[1], client_error("InternalServerError"), after=after)
    else:
        getattr(aws, where).fail_on(call, client_error("InternalServerError"), after=after)
    code, out = run(aws, "--rollback", "--apply")
    assert code == 1, out
    code, out = run(aws, "--apply")
    assert code == 3 and "a rollback of this migration has started; run --rollback again" in out
    code, out = run(aws, "--rollback", "--apply")
    assert code == 0, out
    back(aws, before)
    assert aws.s3.tree(f"{LEGACY}/")["notes/new.md"] == "written after the cut-over"
    assert aws.secrets.live(f"openclaw/user/{LEGACY}/GITHUB_TOKEN") == "ghp-rotated"


def test_rollback_waits_for_a_running_turn_and_changes_nothing():
    aws = world()
    assert run(aws, "--apply")[0] == 0
    session(aws).update(activeAdmissions={"token-3"}, inFlight=1)
    before = aws.state()
    code, out = run(aws, "--rollback", "--apply")
    assert code == 3 and "rollback freeze: the route holds 1 invocation admission" in out, out
    assert "Nothing was changed" in out and aws.state() == before
    aws.clock.advance(14 * DAY)
    del session(aws)["activeAdmissions"], session(aws)["inFlight"]
    assert run(aws, "--cleanup", "--apply")[0] == 0  # the refused rollback left nothing behind


def test_there_is_nothing_to_roll_back_without_a_migration():
    aws = world()
    code, out = run(aws, "--rollback", "--apply")
    assert code == 3 and "there is no migration to roll back" in out
    assert aws.log.mutating() == []


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_is_refused_during_the_hold_and_deletes_the_old_data_after_it():
    aws = world()
    before_theirs = theirs(aws)
    assert run(aws, "--apply")[0] == 0
    before = aws.state()
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 3 and "the hold lasts until" in out and "--rollback can still restore the old data" in out
    assert aws.state() == before
    aws.clock.advance(14 * DAY)
    code, out = run(aws, "--cleanup")
    # 5 versions and 1 delete marker under <old>/, 1 version under _system/<old>/.
    assert code == 0 and "would delete 7 object versions under the old prefixes, 2 secrets" in out, out
    assert aws.state() == before
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 0, out
    assert aws.s3.count(f"{LEGACY}/") == aws.s3.count(f"_system/{LEGACY}/") == 0
    old_secrets = [n for n in aws.secrets.secrets
                   if n.startswith(f"openclaw/user/{LEGACY}/") or n == f"openclaw/cognito-user/{POOL}/{LEGACY}"]
    assert len(old_secrets) == 4 and all(aws.secrets.secrets[n].get("DeletedDate") for n in old_secrets)
    assert RUNTIME not in aws.cognito.users
    assert f"{LEGACY}-1730000000000" not in aws.logs.streams and f"{USER}-1730000200000" in aws.logs.streams
    assert aws.identity.get(f"NS#{LEGACY}", "OWNER") is None
    assert migration(aws)["state"] == "CLEANED"
    assert aws.s3.tree(f"{NEW}/") == TREE and runtime_routing.resolve_route(aws.identity, USER).namespace == NEW
    assert theirs(aws) == before_theirs
    for name in (f"openclaw/user/{LEGACY}/OPENAI_API_KEY", f"openclaw/cognito-user/{POOL}/{LEGACY}"):
        assert aws.secrets.secrets[name].get("DeletedDate"), name  # 7-day recovery window
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 3 and "cleanup needs a migration that finished" in out


def test_cleanup_is_refused_while_something_wrote_under_the_old_prefix_after_the_cutover():
    aws = world()
    assert run(aws, "--apply")[0] == 0
    aws.s3.put(f"{LEGACY}/notes/stray.md", "written by a container that was still running")
    aws.clock.advance(14 * DAY)
    before = aws.state()
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 3 and "1 object under the old prefixes was written after the cut-over was verified" in out
    assert aws.state() == before


def test_an_interrupted_cleanup_finishes_when_run_again():
    aws = world()
    assert run(aws, "--apply")[0] == 0
    aws.clock.advance(14 * DAY)
    aws.identity.fail_on("update_item", (f"USER#{USER}", "NS_MIGRATION"), client_error("InternalServerError"))
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 1, out
    assert aws.identity.get(f"NS#{LEGACY}", "OWNER") is None and migration(aws)["state"] == "MOVED"
    code, out = run(aws, "--cleanup", "--apply")
    assert code == 0 and "recorded the finished cleanup" in out, out
    assert migration(aws)["state"] == "CLEANED"


def test_a_second_migration_moves_on_from_the_opaque_namespace(allocate):
    allocate += [NEW, NEWER]
    aws = world()
    assert run(aws, "--apply")[0] == 0
    aws.clock.advance(14 * DAY)
    assert run(aws, "--cleanup", "--apply")[0] == 0
    code, out = run(aws, "--apply")
    assert code == 0, out
    assert session(aws)["namespace"] == NEWER and aws.s3.tree(f"{NEWER}/") == TREE
    assert all(f"openclaw-{NEWER}-{sid}" in aws.scheduler.schedules for sid in SCHEDULES)
    assert aws.secrets.secrets[f"openclaw/user/{NEWER}/OPENAI_API_KEY"]["Tags"] == tags(NEWER)
    aws.cognito.users.add(NEW)  # created by the bridge for the first opaque namespace
    aws.clock.advance(14 * DAY)
    assert run(aws, "--cleanup", "--apply")[0] == 0
    assert NEW not in aws.cognito.users and aws.identity.get(f"NS#{NEW}", "OWNER") is None


# ---------------------------------------------------------------------------
# scripts/delete_user_data.py after a migration
# ---------------------------------------------------------------------------


def delete(aws):
    out = io.StringIO()
    code = delete_user_data.main(["--region", "us-west-2", "--env-suffix", "", "--user-id", USER, "--apply"],
                                 aws=aws, out=out, today=datetime.date(2026, 11, 5))
    return code, out.getvalue()


def nothing_of_ours(aws):
    left = [k for k in aws.identity.items if USER in k[0] or k[0] in (
        f"CHANNEL#{RUNTIME}", f"ACTOR#{RUNTIME}", f"ALLOW#{RUNTIME}") or k[0].startswith("NS#") and
        aws.identity.items[k].get("userId") == USER]
    left += [k for k in aws.s3.versions if k.startswith((f"{LEGACY}/", f"_system/{LEGACY}/", "ns_", "_system/ns_"))]
    left += [n for n in aws.scheduler.schedules if not n.startswith(f"openclaw-{OTHER_NS}-")]
    left += [n for n, s in aws.secrets.secrets.items() if OTHER_NS not in n and not s.get("DeletedDate")]
    left += [u for u in aws.cognito.users if u != OTHER]
    left += [s for s in aws.logs.streams if OTHER_NS not in s]
    return left


@pytest.mark.parametrize("stage", ["backfilled", "moved", "rolled back", "cleaned", "cleaned twice"])
def test_delete_user_data_finds_every_namespace_of_a_migrated_user(stage, allocate):
    allocate += [NEW, NEWER]
    aws = world()
    before_theirs = theirs(aws)
    if stage != "backfilled":
        assert run(aws, "--apply")[0] == 0
        aws.cognito.users.add(NEW)
    if stage == "rolled back":
        assert run(aws, "--rollback", "--apply")[0] == 0
    if stage.startswith("cleaned"):
        aws.clock.advance(14 * DAY)
        assert run(aws, "--cleanup", "--apply")[0] == 0
    if stage == "cleaned twice":
        assert run(aws, "--apply")[0] == 0
        aws.clock.advance(14 * DAY)
        assert run(aws, "--cleanup", "--apply")[0] == 0
    code, out = delete(aws)
    assert code == 0, out
    assert nothing_of_ours(aws) == []
    assert theirs(aws) == before_theirs
