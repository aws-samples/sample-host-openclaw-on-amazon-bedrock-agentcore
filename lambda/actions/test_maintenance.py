import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest


ACTIONS_DIR = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ACTIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models = load("action_models", "models.py")
state_module = load("action_state_machine", "state_machine.py")
maintenance = load("action_maintenance", "maintenance.py")
NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


def action(state, *, action_id="action_12345678", ttl=None, revision=4):
    return {
        "PK": "USER#founder-1",
        "SK": f"ACTION#{action_id}",
        "actionId": action_id,
        "userId": "founder-1",
        "state": state.value,
        "revision": revision,
        "draftRevision": 3,
        "approvalExpiresAt": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl": ttl if ttl is not None else int((NOW + timedelta(days=14)).timestamp()),
    }


class Repository:
    def __init__(self, record):
        self.record = dict(record)
        self.transitions = []

    def get(self, *, action_id, user_id):
        if self.record["actionId"] == action_id and self.record["userId"] == user_id:
            return dict(self.record)
        return None

    def transition(self, **kwargs):
        self.transitions.append(kwargs)
        if (
            self.record["state"] != kwargs["expected_state"].value
            or self.record["revision"] != kwargs["expected_revision"]
        ):
            raise state_module.ConcurrentActionUpdate("lost race")
        self.record.update(kwargs["updates"])
        self.record["state"] = kwargs["target_state"].value
        self.record["revision"] += 1
        self.record["lastTransitionId"] = kwargs["transition_id"]
        return dict(self.record)


class Reconciler:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def lifecycle(record, reconciler=None):
    repository = Repository(record)
    reconciler = reconciler or Reconciler()
    machine = state_module.ActionStateMachine(
        repository,
        operation_id_factory=lambda: "maint_1234567890abcdef",
    )
    service = maintenance.ActionLifecycleMaintainer(
        repository=repository,
        state_machine=machine,
        reconciler_factory=lambda _record: reconciler,
        now=lambda: NOW,
    )
    return service, repository, reconciler


@pytest.mark.parametrize(
    "state,target",
    [
        (models.ActionState.PREPARED, models.ActionState.CANCELLED),
        (models.ActionState.APPROVAL_PENDING, models.ActionState.EXPIRED),
        (models.ActionState.APPROVED, models.ActionState.EXPIRED),
    ],
)
def test_stale_non_effect_actions_expire_deterministically_without_provider(state, target):
    record = action(state, ttl=int(NOW.timestamp()))
    service, repository, reconciler = lifecycle(record)

    outcome = service.maintain(
        action_id=record["actionId"],
        user_id=record["userId"],
    )

    assert outcome == target.value.lower()
    assert repository.record["state"] == target.value
    assert reconciler.calls == []
    assert repository.transitions[0]["transition_id"].startswith("maint_")


def test_dispatching_is_observed_only_then_becomes_uncertain_without_resend():
    record = action(models.ActionState.DISPATCHING)
    service, repository, reconciler = lifecycle(record)

    outcome = service.maintain(
        action_id=record["actionId"],
        user_id=record["userId"],
    )

    assert outcome == "uncertain"
    assert reconciler.calls == [
        {"action_id": record["actionId"], "user_id": record["userId"]}
    ]
    assert repository.record["state"] == "UNCERTAIN"
    assert repository.record["uncertaintyReason"] == "provider-outcome-unproven"


@pytest.mark.parametrize(
    "state",
    [models.ActionState.DISPATCHING, models.ActionState.UNCERTAIN],
)
def test_expired_effect_state_is_never_queried_or_resurrected(state):
    record = action(state, ttl=int(NOW.timestamp()))
    service, repository, reconciler = lifecycle(record)

    outcome = service.maintain(
        action_id=record["actionId"],
        user_id=record["userId"],
    )

    assert outcome == "retention-expired"
    assert reconciler.calls == []
    assert repository.transitions == []
    assert repository.record["ttl"] == int(NOW.timestamp())


def test_reconciler_result_is_not_trusted_without_a_strong_confirmed_state():
    record = action(models.ActionState.DISPATCHING)
    service, repository, _ = lifecycle(record, Reconciler(result=object()))

    outcome = service.maintain(
        action_id=record["actionId"],
        user_id=record["userId"],
    )

    assert outcome == "uncertain"
    assert repository.record["state"] == "UNCERTAIN"


class PageTable:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs.get("ExclusiveStartKey")
        return self.pages[None if key is None else (key["PK"], key["SK"])]


class CursorStore:
    def __init__(self):
        self.cursor = None
        self.generation = 0
        self.saves = []

    def load(self):
        return maintenance.CursorLease(self.cursor, self.generation)

    def save(self, lease, cursor):
        assert lease == maintenance.CursorLease(self.cursor, self.generation)
        self.generation += 1
        self.cursor = cursor
        self.saves.append(cursor)


class Lifecycle:
    def __init__(self, poison=None):
        self.poison = poison
        self.calls = []

    def maintain(self, *, action_id, user_id):
        self.calls.append((action_id, user_id))
        if action_id == self.poison:
            raise RuntimeError("poison action")
        return "active"


def ref(index):
    action_id = f"action_{index:08d}"
    return {
        "PK": "USER#founder-1",
        "SK": f"ACTION#{action_id}",
        "actionId": action_id,
        "userId": "founder-1",
    }


def test_bounded_pagination_surfaces_poison_after_advancing_past_the_bounded_page():
    first_cursor = {"PK": "USER#founder-1", "SK": "ACTION#action_00000024"}
    table = PageTable(
        {
            None: {"Items": [ref(i) for i in range(25)], "LastEvaluatedKey": first_cursor},
            (first_cursor["PK"], first_cursor["SK"]): {"Items": [ref(25), ref(26)]},
        }
    )
    source = maintenance.DynamoActionPageSource(table, page_size=25)
    cursors = CursorStore()
    worker = Lifecycle(poison="action_00000025")
    runner = maintenance.ActionMaintenanceRunner(
        page_source=source,
        lifecycle=worker,
        cursor_store=cursors,
        max_pages=1,
    )

    first = runner.run()
    with pytest.raises(
        maintenance.ActionMaintenanceError,
        match="1 action maintenance item failed",
    ):
        runner.run()

    assert first == {
        "status": "ok",
        "processed": 25,
        "failed": 0,
        "hasMore": True,
    }
    assert worker.calls[-1] == ("action_00000026", "founder-1")
    assert table.calls[1]["ExclusiveStartKey"] == first_cursor
    assert cursors.saves == [first_cursor, None]

    recovered = runner.run()
    assert recovered == {
        "status": "ok",
        "processed": 25,
        "failed": 0,
        "hasMore": True,
    }
    assert table.calls[2].get("ExclusiveStartKey") is None


def test_cursor_is_committed_after_each_page_before_a_later_page_failure():
    first_cursor = {"PK": "USER#founder-1", "SK": "ACTION#action_00000000"}

    class LaterPageFails(PageTable):
        def scan(self, **kwargs):
            if kwargs.get("ExclusiveStartKey") is not None:
                raise TimeoutError("simulated invocation deadline")
            return {
                "Items": [ref(0)],
                "LastEvaluatedKey": first_cursor,
            }

    cursors = CursorStore()
    runner = maintenance.ActionMaintenanceRunner(
        page_source=maintenance.DynamoActionPageSource(
            LaterPageFails({}), page_size=1
        ),
        lifecycle=Lifecycle(),
        cursor_store=cursors,
        max_pages=2,
    )

    with pytest.raises(maintenance.ActionMaintenanceError, match="scan failed"):
        runner.run()

    assert cursors.cursor == first_cursor
    assert cursors.generation == 1
    assert cursors.saves == [first_cursor]


def test_page_source_rejects_repeated_or_cross_shape_cursor():
    source = maintenance.DynamoActionPageSource(PageTable({}), page_size=25)
    for invalid in [True, {}, {"PK": "USER#x"}, {"PK": "USER#x", "SK": 1}]:
        with pytest.raises(maintenance.ActionMaintenanceError):
            source.page(cursor=invalid)


class CursorTable:
    def __init__(self, *, lose_response=False):
        self.item = None
        self.lose_response = lose_response
        self.updates = []

    def get_item(self, **_kwargs):
        return {} if self.item is None else {"Item": dict(self.item)}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        current = 0 if self.item is None else self.item["generation"]
        if current != values[":expectedGeneration"]:
            raise RuntimeError("conditional conflict")
        self.item = {
            "PK": "SYSTEM#ACTION_MAINTENANCE",
            "SK": "CURSOR#V1",
            "kind": values[":kind"],
            "cursor": values[":cursor"],
            "generation": values[":nextGeneration"],
            "updatedAt": values[":updatedAt"],
        }
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after commit")
        return {"Attributes": dict(self.item)}


def test_durable_cursor_reconciles_response_loss_and_rejects_stale_writer():
    table = CursorTable(lose_response=True)
    store = maintenance.DynamoActionCursorStore(table, now=lambda: NOW)
    lease = store.load()
    cursor = {"PK": "USER#founder-1", "SK": "ACTION#action_12345678"}

    store.save(lease, cursor)

    assert store.load() == maintenance.CursorLease(cursor, 1)
    with pytest.raises(maintenance.ActionMaintenanceError, match="write failed"):
        store.save(lease, None)
