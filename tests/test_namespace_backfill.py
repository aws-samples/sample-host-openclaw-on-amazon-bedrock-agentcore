"""Tests for scripts/namespace_backfill.py against the stateful fakes in tests/_namespace_fakes.py.

No AWS calls. All IDs are synthetic.
"""

import hashlib
import io
import sys
from pathlib import Path

import pytest

from tests._namespace_fakes import Aws, client_error, enrol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "lambda" / "shared" / "python"))
import namespace_backfill as backfill  # noqa: E402
import runtime_routing  # noqa: E402


def derived(actor):
    return f"user_{hashlib.sha256(actor.encode()).hexdigest()[:16]}"


ACTORS = ["telegram:100000001", "slack:U0SYNTH001", "slack_channel:C0SYNTH01", "feishu_group:oc_0123abcdef",
          "line:U0123456789abcdef0123456789abcdef"]
USERS = [derived(actor) for actor in ACTORS]
FIRST, FIRST_USER, FIRST_NS = ACTORS[0], USERS[0], "telegram_100000001"
OPAQUE = "ns_" + "b" * 26


def world():
    aws = Aws()
    for actor, user in zip(ACTORS, USERS):
        enrol(aws, actor, user)
    aws.identity.put(PK="USER#someone-by-hand", SK="SESSION", actorId="telegram:1")  # not an internal user ID
    return aws


def run(aws, *args):
    out = io.StringIO()
    code = backfill.main(["--region", "us-west-2", "--env-suffix", "", *args], aws=aws, out=out)
    return code, out.getvalue()


def session(aws, user=FIRST_USER):
    return aws.identity.get(f"USER#{user}", "SESSION")


def test_dry_run_changes_nothing():
    aws = world()
    before = aws.state()
    code, out = run(aws)
    assert code == 0, out
    assert "DRY RUN" in out and "would store: 5" in out
    assert aws.log.mutating() == [] and aws.state() == before


def test_apply_stores_the_namespace_each_route_already_uses():
    aws = world()
    routes = {user: runtime_routing.resolve_route(aws.identity, user) for user in USERS}
    code, out = run(aws, "--apply")
    assert code == 0 and "stored: 5" in out, out
    for actor, user in zip(ACTORS, USERS):
        namespace = actor.replace(":", "_")
        assert session(aws, user)["namespace"] == namespace
        assert aws.identity.get(f"NS#{namespace}", "OWNER")["userId"] == user
        route = runtime_routing.resolve_route(aws.identity, user)
        # Same namespace as before, now read from the route; the generation is unchanged.
        assert route.namespace == routes[user].namespace and route.namespace_stored
        assert route.generation == routes[user].generation
    calls = len(aws.log.mutating())
    code, out = run(aws, "--apply")
    assert code == 0 and "already stored: 5" in out and len(aws.log.mutating()) == calls


def test_the_owner_record_is_written_before_the_route_so_a_rerun_finishes():
    aws = world()
    aws.identity.fail_on("update_item", (f"USER#{FIRST_USER}", "SESSION"), client_error("InternalServerError"))
    code, out = run(aws, "--apply")
    assert code == 1 and "FAILED: InternalServerError. Re-run the same command to continue." in out
    assert aws.identity.get(f"NS#{FIRST_NS}", "OWNER")["userId"] == FIRST_USER
    assert "namespace" not in session(aws)
    assert runtime_routing.resolve_route(aws.identity, FIRST_USER).namespace == FIRST_NS  # still derived
    code, out = run(aws, "--apply")
    assert code == 0, out
    assert all("namespace" in session(aws, user) for user in USERS)


def test_an_owner_record_of_another_user_is_a_conflict():
    aws = world()
    aws.identity.put(PK=f"NS#{FIRST_NS}", SK="OWNER", userId=USERS[1])
    code, out = run(aws, "--apply")
    assert code == 3 and "conflict: 1 (" in out and FIRST_USER in out, out
    assert "namespace" not in session(aws)
    assert aws.identity.get(f"NS#{FIRST_NS}", "OWNER")["userId"] == USERS[1]
    assert "stored: 4" in out


def test_a_stored_namespace_without_its_owner_record_is_a_conflict():
    aws = world()
    session(aws)["namespace"] = OPAQUE  # e.g. written inside the user's own partition
    code, out = run(aws, "--user-id", FIRST_USER, "--apply")
    assert code == 3 and "conflict: 1" in out, out
    aws.identity.put(PK=f"NS#{OPAQUE}", SK="OWNER", userId=FIRST_USER)
    code, out = run(aws, "--user-id", FIRST_USER, "--apply")
    assert code == 0 and "already stored: 1" in out, out


@pytest.mark.parametrize("tamper", [
    lambda aws: aws.identity.items.pop((f"ACTOR#{FIRST}", "OWNER")),
    lambda aws: aws.identity.get(f"ACTOR#{FIRST}", "OWNER").update(userId=USERS[1]),
    lambda aws: aws.identity.get(f"ACTOR#{FIRST}", "OWNER").update(runtimeActorId=ACTORS[1]),
    lambda aws: session(aws).update(actorId=ACTORS[1]),
], ids=["missing", "other-user", "linked-elsewhere", "swapped"])
def test_a_route_whose_actor_the_owner_record_does_not_confirm_is_skipped(tamper):
    aws = world()
    tamper(aws)
    code, out = run(aws, "--apply")
    assert code == 3 and "unconfirmed: 1" in out and FIRST_USER in out, out
    assert "namespace" not in session(aws)
    assert not [k for k in aws.identity.items if k[0].startswith("NS#") and
                aws.identity.items[k]["userId"] == FIRST_USER]


def test_busy_and_inactive_routes_are_skipped_and_reported():
    aws = world()
    session(aws).update(activeAdmissions={"token-1"}, inFlight=1)
    session(aws, USERS[1])["routeStatus"] = "DRAINING"
    session(aws, USERS[2])["routeStatus"] = "MIGRATING"
    code, out = run(aws, "--apply")
    assert code == 3, out  # inactive routes need attention first
    assert "busy: 1" in out and "inactive: 2" in out and "stored: 2" in out
    assert all("namespace" not in session(aws, user) for user in USERS[:3])
    session(aws, USERS[1])["routeStatus"] = session(aws, USERS[2])["routeStatus"] = "ACTIVE"
    code, out = run(aws, "--apply")
    assert code == 1 and "busy: 1" in out, out  # re-run later
    del session(aws)["activeAdmissions"], session(aws)["inFlight"]
    assert run(aws, "--apply")[0] == 0


def test_a_route_that_changes_while_it_is_stored_is_busy():
    aws = world()
    real = aws.identity.update_item

    def admitted_first(**kwargs):
        aws.identity.update_item = real
        session(aws).update(activeAdmissions={"token-2"}, inFlight=1)  # a Router admission wins the race
        return real(**kwargs)

    aws.identity.update_item = admitted_first
    code, out = run(aws, "--user-id", FIRST_USER, "--apply")
    assert code == 1 and "busy: 1" in out, out
    assert "namespace" not in session(aws)


def test_user_id_and_limit_select_routes():
    aws = world()
    code, out = run(aws, "--user-id", USERS[3], "--apply")
    assert code == 0 and "stored: 1" in out, out
    assert [user for user in USERS if "namespace" in session(aws, user)] == [USERS[3]]
    code, out = run(aws, "--limit", "2", "--apply")
    assert code == 0 and "stored: 2" in out and "already stored: 1" in out, out
    assert sum("namespace" in session(aws, user) for user in USERS) == 3


def test_output_names_users_by_internal_id_only():
    aws = world()
    aws.identity.put(PK=f"NS#{FIRST_NS}", SK="OWNER", userId=USERS[1])
    for args in ((), ("--apply",)):
        code, out = run(aws, *args)
        assert FIRST_USER in out
        for actor in ACTORS:
            assert actor.split(":", 1)[1] not in out, actor


@pytest.mark.parametrize("args", [["--user-id", "user_xyz"], ["--limit", "0"], ["--env-suffix", "Bad_Suffix"]])
def test_bad_arguments_exit_2(args):
    with pytest.raises(SystemExit) as raised:
        backfill.main(["--region", "us-west-2", *args], aws=world(), out=io.StringIO())
    assert raised.value.code == 2
