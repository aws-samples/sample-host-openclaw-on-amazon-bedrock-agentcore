"""RED-first exact approval and uncertain-effect tests for schedule control."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from scheduler.conftest import DELIVERY_TARGET, reminder_definition
from scheduler.models import SchedulePayloadV1, build_schedule_spec, derive_schedule_id
from scheduler.proposals import (
    ScheduleProposalRecordV1,
    build_cancel_schedule_proposal,
    build_create_schedule_proposal,
)
from scheduler.control import (
    ControlOutcome,
    DynamoScheduleControlRepository,
    EventBridgeSchedulerAdapter,
    ProposalSnapshot,
    ScheduleControlService,
    ScheduleSnapshot,
    configure_service_factory,
    handle_control,
    lambda_handler,
)


NOW = 1_800_000_000
CATALOG_DIGEST = "a" * 64


class ProviderUncertain(RuntimeError):
    pass


class MemoryControlRepository:
    def __init__(self, records=()):
        self.proposals = {
            record.proposal_id: {
                "record": record,
                "state": "PENDING",
                "version": 1,
                "outcome": None,
            }
            for record in records
        }
        self.schedules = {}
        self.fail_next_finish = False

    def strong_read_proposal(self, *, user_id, proposal_ref):
        row = self.proposals.get(proposal_ref)
        if row is None or row["record"].user_id != user_id:
            return None
        return ProposalSnapshot(
            record=row["record"],
            state=row["state"],
            version=row["version"],
            outcome=row["outcome"],
        )

    def strong_read_schedule(self, schedule_id):
        row = self.schedules.get(schedule_id)
        if row is None:
            return None
        return ScheduleSnapshot(
            spec=row["spec"], delivery_target=row["deliveryTarget"]
        )

    def claim_create(self, snapshot, spec, delivery_target, *, now):
        row = self.proposals[snapshot.record.proposal_id]
        if row["state"] != "PENDING" or row["version"] != snapshot.version:
            return False
        if spec.schedule_id in self.schedules:
            return False
        row.update(state="APPLYING", version=2)
        self.schedules[spec.schedule_id] = {
            "spec": spec,
            "deliveryTarget": dict(delivery_target),
        }
        return True

    def claim_cancel(self, snapshot, current, cancelled, *, now):
        row = self.proposals[snapshot.record.proposal_id]
        schedule = self.schedules.get(current.spec.schedule_id)
        if (
            row["state"] != "PENDING"
            or row["version"] != snapshot.version
            or schedule is None
            or schedule["spec"] != current.spec
        ):
            return False
        row.update(state="APPLYING", version=2)
        schedule["spec"] = cancelled
        return True

    def finish_proposal(self, snapshot, *, status, outcome, now):
        if self.fail_next_finish:
            self.fail_next_finish = False
            return False
        row = self.proposals[snapshot.record.proposal_id]
        expected = "UNCERTAIN" if snapshot.state == "UNCERTAIN" else "APPLYING"
        if row["state"] != expected:
            return False
        row.update(state=status, version=row["version"] + 1, outcome=outcome)
        return True

    def reject_proposal(self, snapshot, *, outcome, now):
        row = self.proposals[snapshot.record.proposal_id]
        if row["state"] != "PENDING" or row["version"] != snapshot.version:
            return False
        row.update(state="REJECTED", version=2, outcome=outcome)
        return True

    def list_user_schedules(self, user_id):
        return tuple(
            ScheduleSnapshot(
                spec=row["spec"], delivery_target=row["deliveryTarget"]
            )
            for row in self.schedules.values()
            if row["spec"].user_id == user_id
        )

    def fence_schedule_for_purge(self, current, *, now):
        row = self.schedules.get(current.spec.schedule_id)
        if row is None or row["spec"] != current.spec:
            return None
        if current.spec.state == "ENABLED":
            row["spec"] = build_schedule_spec(
                schedule_id=current.spec.schedule_id,
                user_id=current.spec.user_id,
                task_type=current.spec.task_type,
                definition=current.spec.definition,
                revision=current.spec.revision + 1,
                state="CANCELLED",
                next_run_at=None,
            )
        return ScheduleSnapshot(
            spec=row["spec"], delivery_target=row["deliveryTarget"]
        )

    def delete_schedule_partition(self, current):
        return self.schedules.pop(current.spec.schedule_id, None) is not None

    def delete_user_proposals(self, user_id):
        for proposal_ref in [
            proposal_ref
            for proposal_ref, row in self.proposals.items()
            if row["record"].user_id == user_id
        ]:
            del self.proposals[proposal_ref]
        return True


class ObservedProvider:
    def __init__(self, repository):
        self.repository = repository
        self.created = []
        self.deleted = []
        self.observed = []
        self.create_error = None
        self.delete_error = None
        self.observation = "UNKNOWN"

    def create_one_time_schedule(self, *, spec, payload):
        # Durable intent and APPLYING proposal must precede the provider call.
        assert self.repository.schedules[spec.schedule_id]["spec"] == spec
        proposal = next(iter(self.repository.proposals.values()))
        assert proposal["state"] == "APPLYING"
        self.created.append((spec, payload))
        if self.create_error is not None:
            raise self.create_error

    def delete_schedule(self, *, schedule_id):
        # Cancellation generation fence must precede provider deletion.
        assert self.repository.schedules[schedule_id]["spec"].state == "CANCELLED"
        self.deleted.append(schedule_id)
        if self.delete_error is not None:
            raise self.delete_error

    def observe_schedule(self, *, schedule_id, expected_payload=None):
        self.observed.append(schedule_id)
        return self.observation


def create_record(*, now=NOW):
    return build_create_schedule_proposal(
        catalog_digest=CATALOG_DIGEST,
        user_id="user_a1",
        invocation_id="invocation_12345678",
        task_type="REMINDER",
        definition=reminder_definition(),
        delivery_target=DELIVERY_TARGET,
        now=now,
        nonce="nonce_create_12345678",
    )


def cancel_record(schedule_id, *, revision=1, now=NOW):
    return build_cancel_schedule_proposal(
        catalog_digest=CATALOG_DIGEST,
        user_id="user_a1",
        invocation_id="invocation_cancel_1234",
        schedule_id=schedule_id,
        revision=revision,
        delivery_target=DELIVERY_TARGET,
        now=now,
        nonce="nonce_cancel_12345678",
    )


def service_for(record, *, clock=lambda: NOW, authority_guard=None):
    repository = MemoryControlRepository([record])
    provider = ObservedProvider(repository)
    service = ScheduleControlService(
        repository=repository,
        provider=provider,
        catalog_digest=CATALOG_DIGEST,
        clock=clock,
        authority_guard=authority_guard or (lambda _user, _operation: None),
        uncertain_errors=(ProviderUncertain,),
    )
    return service, repository, provider


def approve(service, record, *, user_id="user_a1", revision=None, args_hash=None):
    return service.approve(
        user_id=user_id,
        proposal_ref=record.proposal_id,
        revision=record.proposal.revision if revision is None else revision,
        args_hash=record.args_hash if args_hash is None else args_hash,
    )


def test_create_approval_commits_exact_intent_before_one_provider_call():
    record = create_record()
    service, repository, provider = service_for(record)

    outcome = approve(service, record)

    assert outcome == ControlOutcome(
        status="SUCCEEDED",
        proposal_ref=record.proposal_id,
        schedule_id=record.schedule_id,
        revision=1,
    )
    assert len(provider.created) == 1
    assert repository.schedules[record.schedule_id]["spec"].state == "ENABLED"
    assert repository.proposals[record.proposal_id]["state"] == "SUCCEEDED"


@pytest.mark.parametrize(
    ("user_id", "revision", "args_hash", "now"),
    [
        ("user_b2", 1, None, NOW),
        ("user_a1", 2, None, NOW),
        ("user_a1", 1, "f" * 64, NOW),
        ("user_a1", 1, None, NOW + 901),
    ],
)
def test_wrong_tenant_revision_hash_or_expiry_makes_zero_provider_calls(
    user_id, revision, args_hash, now
):
    record = create_record()
    service, repository, provider = service_for(record, clock=lambda: now)

    with pytest.raises(Exception):
        approve(
            service,
            record,
            user_id=user_id,
            revision=revision,
            args_hash=record.args_hash if args_hash is None else args_hash,
        )

    assert provider.created == []
    assert provider.deleted == []
    assert repository.schedules == {}


def test_repeated_exact_approval_returns_terminal_result_without_provider_replay():
    record = create_record()
    service, _, provider = service_for(record)

    first = approve(service, record)
    second = approve(service, record)

    assert second == first
    assert len(provider.created) == 1


def test_cancel_approval_stales_ingress_before_deleting_provider_schedule():
    initial = create_record()
    schedule = build_schedule_spec(
        schedule_id=initial.schedule_id,
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        revision=1,
        state="ENABLED",
    )
    record = cancel_record(schedule.schedule_id)
    service, repository, provider = service_for(record)
    repository.schedules[schedule.schedule_id] = {
        "spec": schedule,
        "deliveryTarget": dict(DELIVERY_TARGET),
    }

    outcome = approve(service, record)

    assert outcome.status == "SUCCEEDED"
    assert outcome.revision == 2
    assert provider.deleted == [schedule.schedule_id]
    assert repository.schedules[schedule.schedule_id]["spec"].state == "CANCELLED"


@pytest.mark.parametrize("operation", ["create", "cancel"])
def test_ambiguous_provider_effect_is_durable_uncertain_and_never_retried(operation):
    initial = create_record()
    if operation == "create":
        record = initial
    else:
        record = cancel_record(initial.schedule_id)
    service, repository, provider = service_for(record)
    if operation == "create":
        provider.create_error = ProviderUncertain("lost create response")
    else:
        repository.schedules[initial.schedule_id] = {
            "spec": build_schedule_spec(
                schedule_id=initial.schedule_id,
                user_id="user_a1",
                task_type="REMINDER",
                definition=reminder_definition(),
                revision=1,
                state="ENABLED",
            ),
            "deliveryTarget": dict(DELIVERY_TARGET),
        }
        provider.delete_error = ProviderUncertain("lost delete response")

    first = approve(service, record)
    second = approve(service, record)

    assert first.status == second.status == "UNCERTAIN"
    assert repository.proposals[record.proposal_id]["state"] == "UNCERTAIN"
    assert len(provider.created) + len(provider.deleted) == 1


@pytest.mark.parametrize(
    ("operation", "observation"),
    [("create", "PRESENT"), ("cancel", "MISSING")],
)
def test_reconcile_only_observes_and_advances_exact_positive_evidence(
    operation, observation
):
    initial = create_record()
    record = initial if operation == "create" else cancel_record(initial.schedule_id)
    service, repository, provider = service_for(record)
    if operation == "cancel":
        repository.schedules[initial.schedule_id] = {
            "spec": build_schedule_spec(
                schedule_id=initial.schedule_id,
                user_id="user_a1",
                task_type="REMINDER",
                definition=reminder_definition(),
                revision=1,
                state="ENABLED",
            ),
            "deliveryTarget": dict(DELIVERY_TARGET),
        }
        provider.delete_error = ProviderUncertain("lost delete response")
    else:
        provider.create_error = ProviderUncertain("lost create response")
    assert approve(service, record).status == "UNCERTAIN"
    provider.create_error = None
    provider.delete_error = None
    provider.observation = observation

    reconciled = service.reconcile(
        user_id="user_a1", proposal_ref=record.proposal_id
    )

    assert reconciled.status == "SUCCEEDED"
    assert provider.observed == [record.schedule_id]
    # Reconciliation never redispatches the effect.
    assert len(provider.created) + len(provider.deleted) == 1


def test_reconcile_recovers_applying_fence_after_terminal_write_was_lost():
    record = create_record()
    service, repository, provider = service_for(record)
    repository.fail_next_finish = True

    with pytest.raises(Exception, match="success"):
        approve(service, record)
    assert repository.proposals[record.proposal_id]["state"] == "APPLYING"
    assert len(provider.created) == 1
    provider.observation = "PRESENT"

    outcome = service.reconcile(
        user_id="user_a1", proposal_ref=record.proposal_id
    )

    assert outcome.status == "SUCCEEDED"
    assert repository.proposals[record.proposal_id]["state"] == "SUCCEEDED"
    assert len(provider.created) == 1


def test_reject_is_exact_one_time_and_never_calls_provider():
    record = create_record()
    service, repository, provider = service_for(record)

    rejected = service.reject(
        user_id="user_a1",
        proposal_ref=record.proposal_id,
        revision=1,
        args_hash=record.args_hash,
    )

    assert rejected.status == "REJECTED"
    assert repository.proposals[record.proposal_id]["state"] == "REJECTED"
    assert provider.created == provider.deleted == []


def test_preview_reparses_frozen_record_and_exposes_the_approval_binding():
    record = create_record()
    service, _, _ = service_for(record)

    preview = service.preview(user_id="user_a1", proposal_ref=record.proposal_id)

    assert preview == {
        "proposalRef": record.proposal_id,
        "operationId": "schedule.propose",
        "scheduleId": record.schedule_id,
        "revision": 1,
        "argsHash": record.args_hash,
        "arguments": record.proposal.arguments,
        "expiresAt": record.expires_at,
        "state": "PENDING",
    }


def test_tampered_catalog_is_rejected_before_provider_or_state_change():
    original = create_record()
    proposal = original.proposal.to_mapping()
    proposal["catalogDigest"] = "b" * 64
    tampered_proposal = type(original.proposal).from_mapping(proposal)
    # The immutable wrapper itself catches the substitution before service use.
    with pytest.raises(ValueError, match="binding"):
        ScheduleProposalRecordV1(
            proposal=tampered_proposal,
            schedule_id=original.schedule_id,
            delivery_target=original.delivery_target,
            created_at=original.created_at,
            binding_hash="0" * 64,
        )


def test_live_authority_is_rechecked_immediately_before_schedule_effect():
    record = create_record()

    def deny(_user_id, _operation_id):
        raise RuntimeError("deletion fence")

    service, repository, provider = service_for(record, authority_guard=deny)

    with pytest.raises(Exception, match="authority"):
        approve(service, record)

    assert repository.proposals[record.proposal_id]["state"] == "PENDING"
    assert repository.schedules == {}
    assert provider.created == provider.deleted == []


def test_account_purge_fences_live_schedules_then_deletes_all_user_records():
    record = create_record()
    service, repository, provider = service_for(record)
    repository.schedules[record.schedule_id] = {
        "spec": build_schedule_spec(
            schedule_id=record.schedule_id,
            user_id="user_a1",
            task_type="REMINDER",
            definition=reminder_definition(),
            revision=1,
            state="ENABLED",
        ),
        "deliveryTarget": dict(DELIVERY_TARGET),
    }

    assert service.purge_user_schedules("user_a1") == 0

    assert provider.deleted == [record.schedule_id]
    assert repository.schedules == {}
    assert repository.proposals == {}


def test_account_purge_keeps_records_and_reports_remaining_on_uncertain_delete():
    record = create_record()
    service, repository, provider = service_for(record)
    repository.schedules[record.schedule_id] = {
        "spec": build_schedule_spec(
            schedule_id=record.schedule_id,
            user_id="user_a1",
            task_type="REMINDER",
            definition=reminder_definition(),
            revision=1,
            state="ENABLED",
        ),
        "deliveryTarget": dict(DELIVERY_TARGET),
    }
    provider.delete_error = ProviderUncertain("lost delete response")

    assert service.purge_user_schedules("user_a1") == 1

    assert repository.schedules[record.schedule_id]["spec"].state == "CANCELLED"
    assert record.proposal_id in repository.proposals


def _proposal_item(record, *, state="PENDING", version=1, outcome=None):
    item = {
        "PK": {"S": f"USER#{record.user_id}"},
        "SK": {"S": f"PROPOSAL#{record.proposal_id}"},
        "proposalUserId": {"S": record.user_id},
        "proposalSortKey": {
            "S": f"{record.created_at:020d}#{record.proposal_id}"
        },
        "recordJson": {
            "S": json.dumps(
                record.to_mapping(), sort_keys=True, separators=(",", ":")
            )
        },
        "state": {"S": state},
        "version": {"N": str(version)},
        "ttl": {"N": str(record.created_at + 90 * 24 * 60 * 60)},
    }
    if outcome is not None:
        item["outcomeJson"] = {
            "S": json.dumps(
                outcome.to_mapping(), sort_keys=True, separators=(",", ":")
            )
        }
    return item


def _schedule_item(spec):
    return {
        "PK": {"S": f"SCHEDULE#{spec.schedule_id}"},
        "SK": {"S": "STATE"},
        "userId": {"S": spec.user_id},
        "scheduleUserId": {"S": spec.user_id},
        "scheduleSortKey": {"S": f"SCHEDULE#{spec.schedule_id}"},
        "recordJson": {
            "S": json.dumps(
                spec.to_mapping(), sort_keys=True, separators=(",", ":")
            )
        },
        "deliveryJson": {
            "S": json.dumps(
                DELIVERY_TARGET, sort_keys=True, separators=(",", ":")
            )
        },
    }


class RecordingDynamo:
    def __init__(self, item=None):
        self.item = item
        self.gets = []
        self.transactions = []
        self.updates = []

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        return {} if self.item is None else {"Item": self.item}

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs)
        return {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}

    def query(self, **kwargs):
        return {"Items": []}

    def batch_write_item(self, **kwargs):
        return {"UnprocessedItems": {}}


def test_dynamo_repository_strong_reads_exact_shared_proposal_and_transacts_intent():
    record = create_record()
    client = RecordingDynamo(_proposal_item(record))
    repository = DynamoScheduleControlRepository(
        client=client, table_name="scheduler-control"
    )

    snapshot = repository.strong_read_proposal(
        user_id="user_a1", proposal_ref=record.proposal_id
    )
    spec = build_schedule_spec(
        schedule_id=record.schedule_id,
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        revision=1,
        state="ENABLED",
    )
    assert repository.claim_create(
        snapshot, spec, DELIVERY_TARGET, now=NOW
    ) is True

    assert client.gets == [
        {
            "TableName": "scheduler-control",
            "Key": {
                "PK": {"S": "USER#user_a1"},
                "SK": {"S": f"PROPOSAL#{record.proposal_id}"},
            },
            "ConsistentRead": True,
        }
    ]
    writes = client.transactions[0]["TransactItems"]
    assert [set(write) for write in writes] == [
        {"Update"},
        {"Put"},
        {"Put"},
    ]
    assert writes[1]["Put"]["Item"]["recordJson"]["S"] == json.dumps(
        spec.to_mapping(), sort_keys=True, separators=(",", ":")
    )
    assert writes[1]["Put"]["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )
    assert writes[2]["Put"]["Item"] == {
        "PK": {"S": "USER#user_a1"},
        "SK": {"S": f"SCHEDULE#{spec.schedule_id}"},
        "recordType": {"S": "SCHEDULE_OWNER"},
        "scheduleId": {"S": spec.schedule_id},
    }


def test_dynamo_repository_rejects_extra_or_cross_tenant_proposal_fields():
    record = create_record()
    poisoned = _proposal_item(record)
    poisoned["credential"] = {"S": "forbidden"}
    repository = DynamoScheduleControlRepository(
        client=RecordingDynamo(poisoned), table_name="scheduler-control"
    )

    with pytest.raises(Exception, match="proposal.*invalid"):
        repository.strong_read_proposal(
            user_id="user_a1", proposal_ref=record.proposal_id
        )


def test_dynamo_purge_uses_both_sparse_indexes_and_deletes_exact_partitions():
    record = create_record()
    spec = build_schedule_spec(
        schedule_id=record.schedule_id,
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        revision=2,
        state="CANCELLED",
        next_run_at=None,
    )

    class PurgeDynamo(RecordingDynamo):
        def __init__(self):
            super().__init__()
            self.queries = []
            self.batches = []

        def get_item(self, **kwargs):
            self.gets.append(kwargs)
            return {"Item": _schedule_item(spec)}

        def query(self, **kwargs):
            self.queries.append(kwargs)
            condition = kwargs.get("KeyConditionExpression")
            values = kwargs.get("ExpressionAttributeValues", {})
            prefix = values.get(":prefix", {}).get("S")
            if condition == "PK = :pk AND begins_with(SK, :prefix)" and prefix == "SCHEDULE#":
                return {
                    "Items": [
                        {
                            "PK": {"S": "USER#user_a1"},
                            "SK": {"S": f"SCHEDULE#{spec.schedule_id}"},
                            "recordType": {"S": "SCHEDULE_OWNER"},
                            "scheduleId": {"S": spec.schedule_id},
                        }
                    ]
                }
            if condition == "PK = :pk AND begins_with(SK, :prefix)" and prefix == "PROPOSAL#":
                return {
                    "Items": [
                        {
                            "PK": {"S": "USER#user_a1"},
                            "SK": {"S": f"PROPOSAL#{record.proposal_id}"},
                        }
                    ]
                }
            return {
                "Items": [
                    {
                        "PK": {"S": f"SCHEDULE#{spec.schedule_id}"},
                        "SK": {"S": "STATE"},
                    },
                    {
                        "PK": {"S": f"SCHEDULE#{spec.schedule_id}"},
                        "SK": {"S": "OCCURRENCE#occ_12345678"},
                    },
                ]
            }

        def batch_write_item(self, **kwargs):
            self.batches.append(kwargs)
            return {"UnprocessedItems": {}}

    client = PurgeDynamo()
    repository = DynamoScheduleControlRepository(
        client=client, table_name="scheduler-control"
    )

    schedules = repository.list_user_schedules("user_a1")
    assert len(schedules) == 1 and schedules[0].spec == spec
    assert repository.delete_schedule_partition(schedules[0]) is True
    assert repository.delete_user_proposals("user_a1") is True

    assert [query.get("IndexName") for query in client.queries] == [None, None, None]
    assert client.queries[0]["ConsistentRead"] is True
    assert client.queries[2]["ConsistentRead"] is True
    deleted_keys = [
        request["DeleteRequest"]["Key"]
        for batch in client.batches
        for request in batch["RequestItems"]["scheduler-control"]
    ]
    assert {
        (key["PK"]["S"], key["SK"]["S"]) for key in deleted_keys
    } == {
        (f"SCHEDULE#{spec.schedule_id}", "STATE"),
        (f"SCHEDULE#{spec.schedule_id}", "OCCURRENCE#occ_12345678"),
        ("USER#user_a1", f"SCHEDULE#{spec.schedule_id}"),
        ("USER#user_a1", f"PROPOSAL#{record.proposal_id}"),
    }


class RecordingSchedulerClient:
    def __init__(self):
        self.creates = []
        self.deletes = []
        self.get_response = None
        self.get_error = None
        self.delete_error = None

    def create_schedule(self, **kwargs):
        self.creates.append(kwargs)
        return {"ScheduleArn": "synthetic"}

    def delete_schedule(self, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append(kwargs)
        return {}

    def get_schedule(self, **kwargs):
        if self.get_error is not None:
            raise self.get_error
        return self.get_response


def test_eventbridge_adapter_uses_opaque_bounded_name_and_exact_ingress_target():
    record = create_record()
    spec = build_schedule_spec(
        schedule_id=record.schedule_id,
        user_id="user_a1",
        task_type="REMINDER",
        definition=reminder_definition(),
        revision=1,
        state="ENABLED",
    )
    payload = SchedulePayloadV1(
        schedule_id=spec.schedule_id,
        generation=1,
        fire_time=spec.next_run_at,
    )
    client = RecordingSchedulerClient()
    adapter = EventBridgeSchedulerAdapter(
        client=client,
        ingress_function_arn=(
            "arn:aws:lambda:eu-west-1:123456789012:function:"
            "personal-operator-scheduler-ingress"
        ),
        invoke_role_arn=(
            "arn:aws:iam::123456789012:role/"
            "personal-operator-scheduler-invoke-eu-west-1"
        ),
        group_name="personal-operator-v1",
    )

    adapter.create_one_time_schedule(spec=spec, payload=payload)

    request = client.creates[0]
    assert request["GroupName"] == "personal-operator-v1"
    assert request["Name"].startswith("po-") and len(request["Name"]) <= 64
    assert request["ScheduleExpression"] == "at(2027-01-15T08:10:00)"
    assert request["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert request["ActionAfterCompletion"] == "DELETE"
    assert request["Target"]["Input"] == payload.to_json()
    assert "user_a1" not in json.dumps(request)
    assert "water the plants" not in json.dumps(request)


def test_eventbridge_observation_requires_matching_opaque_target_or_not_found():
    record = create_record()
    client = RecordingSchedulerClient()
    adapter = EventBridgeSchedulerAdapter(
        client=client,
        ingress_function_arn=(
            "arn:aws:lambda:eu-west-1:123456789012:function:"
            "personal-operator-scheduler-ingress"
        ),
        invoke_role_arn=(
            "arn:aws:iam::123456789012:role/"
            "personal-operator-scheduler-invoke-eu-west-1"
        ),
        group_name="personal-operator-v1",
    )
    name = adapter.provider_name(record.schedule_id)
    client.get_response = {
        "Name": name,
        "GroupName": "personal-operator-v1",
        "Target": {
            "Arn": adapter.ingress_function_arn,
            "RoleArn": adapter.invoke_role_arn,
            "Input": SchedulePayloadV1(
                schedule_id=record.schedule_id,
                generation=1,
                fire_time=NOW + 600,
            ).to_json(),
        },
    }
    expected = SchedulePayloadV1(
        schedule_id=record.schedule_id,
        generation=1,
        fire_time=NOW + 600,
    )
    assert adapter.observe_schedule(
        schedule_id=record.schedule_id, expected_payload=expected
    ) == "PRESENT"
    client.get_response["Target"]["Input"] = SchedulePayloadV1(
        schedule_id=record.schedule_id,
        generation=2,
        fire_time=NOW + 600,
    ).to_json()
    assert adapter.observe_schedule(
        schedule_id=record.schedule_id, expected_payload=expected
    ) == "UNKNOWN"
    client.get_response["Target"]["Input"] = SchedulePayloadV1(
        schedule_id=derive_schedule_id("user_b2", "nonce_other_12345678"),
        generation=1,
        fire_time=NOW + 600,
    ).to_json()
    assert adapter.observe_schedule(
        schedule_id=record.schedule_id, expected_payload=expected
    ) == "UNKNOWN"

    client.get_error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        "GetSchedule",
    )
    assert adapter.observe_schedule(
        schedule_id=record.schedule_id, expected_payload=expected
    ) == "MISSING"
    client.delete_error = client.get_error
    # Account deletion and cancellation reconciliation treat exact absence as
    # positive deletion evidence, not an uncertain failure.
    adapter.delete_schedule(schedule_id=record.schedule_id)


def test_handler_accepts_only_exact_trusted_control_commands():
    record = create_record()
    service, _, provider = service_for(record)
    preview = handle_control(
        {
            "action": "PREVIEW",
            "userId": "user_a1",
            "proposalRef": record.proposal_id,
        },
        service,
    )
    assert preview["state"] == "PENDING"

    for poisoned in (
        {"action": "PREVIEW", "userId": "user_a1"},
        {
            "action": "PREVIEW",
            "userId": "user_a1",
            "proposalRef": record.proposal_id,
            "credential": "forbidden",
        },
        {
            "action": "APPROVE",
            "userId": "user_a1",
            "proposalRef": record.proposal_id,
            "revision": 1,
        },
    ):
        with pytest.raises(Exception):
            handle_control(poisoned, service)
    assert provider.created == []


def test_lambda_handler_uses_injected_service_without_aws_clients():
    record = create_record()
    service, _, provider = service_for(record)
    configure_service_factory(lambda: service)
    try:
        response = lambda_handler(
            {
                "action": "APPROVE",
                "userId": "user_a1",
                "proposalRef": record.proposal_id,
                "revision": 1,
                "argsHash": record.args_hash,
            },
            None,
        )
    finally:
        configure_service_factory(None)
    assert response["status"] == "SUCCEEDED"
    assert len(provider.created) == 1
