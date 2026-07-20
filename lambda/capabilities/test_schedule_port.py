from __future__ import annotations

import itertools
import json

import pytest

from capabilities.schedule_port import (
    DynamoPortableScheduleProjectionReader,
    DynamoScheduleCapabilityPort,
    DynamoScheduleDefinitionReader,
)
from capabilities.retention import (
    derive_deletion_subject_binding,
    subject_partition_key,
)
from scheduler.models import build_schedule_spec


NOW = 1_800_000_000


class MemorySchedulerClient:
    def __init__(self):
        self.items = {}
        self.query_calls = []
        self.get_calls = []
        self.put_calls = []

    @staticmethod
    def _key(raw):
        return raw["PK"]["S"], raw["SK"]["S"]

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        partition = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        items = sorted(
            (
                dict(item)
                for (pk, sk), item in self.items.items()
                if pk == partition and sk.startswith(prefix)
            ),
            key=lambda item: item["SK"]["S"],
        )
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            start_sk = start["SK"]["S"]
            items = [item for item in items if item["SK"]["S"] > start_sk]
        limit = kwargs.get("Limit", len(items))
        page = items[:limit]
        response = {"Items": page}
        if len(items) > limit:
            response["LastEvaluatedKey"] = {
                "PK": dict(page[-1]["PK"]),
                "SK": dict(page[-1]["SK"]),
            }
        return response

    def seed_owner(self, *, user_id, schedule_id):
        self.items[(f"USER#{user_id}", f"SCHEDULE#{schedule_id}")] = {
            "PK": {"S": f"USER#{user_id}"},
            "SK": {"S": f"SCHEDULE#{schedule_id}"},
            "recordType": {"S": "SCHEDULE_OWNER"},
            "scheduleId": {"S": schedule_id},
        }

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        item = self.items.get(self._key(kwargs["Key"]))
        return {} if item is None else {"Item": dict(item)}

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)
        key = self._key(kwargs["Item"])
        if key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )
        self.items[key] = dict(kwargs["Item"])
        return {}

    def seed_schedule(self, spec):
        state = {
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
                "S": '{"actorId":"telegram:1","chatId":"1"}'
            },
        }
        if spec.state != "ENABLED":
            state["ttl"] = {"N": str(NOW + 90 * 86400)}
        self.items[(f"SCHEDULE#{spec.schedule_id}", "STATE")] = state
        self.seed_owner(user_id=spec.user_id, schedule_id=spec.schedule_id)

    def seed_delivery(self, *, user_id="user_alpha", invocation_id="invocation_12345678"):
        binding = derive_deletion_subject_binding(user_id)
        key = (subject_partition_key(user_id), f"DELIVERY#{invocation_id}")
        self.items[key] = {
            "PK": {"S": key[0]},
            "SK": {"S": key[1]},
            "ownerBinding": {"S": binding},
            "recordJson": {
                "S": json.dumps(
                    {
                        "schema": "personal-operator.turn-delivery-context.v1",
                        "userId": user_id,
                        "invocationId": invocation_id,
                        "channel": "telegram",
                        "actorId": "telegram:1",
                        "chatId": "1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
            "ttl": {"N": str(NOW + 300)},
            "version": {"N": "1"},
        }


def _port(client, *, nonce_factory=None):
    nonces = iter(["nonce_12345678", "nonce_87654321"])
    return DynamoScheduleCapabilityPort(
        client=client,
        table_name="personal-operator-scheduler-control",
        authority_table_name="personal-operator-capability-state",
        catalog_digest="c" * 64,
        clock=lambda: NOW,
        nonce_factory=nonce_factory or (lambda: next(nonces)),
    )


def _spec(user_id="user_alpha"):
    return build_schedule_spec(
        schedule_id="schedule_12345678",
        user_id=user_id,
        task_type="REMINDER",
        definition={
            "message": "review notes",
            "runAt": NOW + 3600,
            "timezone": "Europe/Tallinn",
        },
        revision=1,
        state="ENABLED",
    )


def _portable_projection_item(*, user_id="user_alpha", schedules=None):
    projection = {
        "schema": "personal-operator.portable-schedule-projection.v1",
        "userId": user_id,
        "generation": 1,
        "bundleHash": "a" * 64,
        "schedules": schedules
        if schedules is not None
        else [
            {
                "scheduleId": "schedule_imported_12345678",
                "userId": user_id,
                "taskType": "READ_ONLY_AGENT_TURN",
                "state": "DISABLED",
            }
        ],
    }
    return {
        "PK": {"S": f"USER#{user_id}"},
        "SK": {"S": "PORTABLE#LIVE_STATE"},
        "recordType": {"S": "PORTABLE_LIVE_STATE_V2"},
        "userId": {"S": user_id},
        "generation": {"N": "1"},
        "liveBundleHash": {"S": "a" * 64},
        "liveScheduleProjectionJson": {
            "S": json.dumps(projection, sort_keys=True, separators=(",", ":"))
        },
    }


def test_portable_projection_reader_is_strong_content_free_and_subject_bound():
    client = MemorySchedulerClient()
    client.items[("USER#user_alpha", "PORTABLE#LIVE_STATE")] = (
        _portable_projection_item()
    )
    reader = DynamoPortableScheduleProjectionReader(
        client=client,
        table_name="personal-operator-control",
    )

    assert reader.disabled_schedule_views("user_alpha") == [
        {
            "scheduleId": "schedule_imported_12345678",
            "userId": "user_alpha",
            "taskType": "READ_ONLY_AGENT_TURN",
            "state": "DISABLED",
        }
    ]
    assert client.get_calls == [
        {
            "TableName": "personal-operator-control",
            "Key": {
                "PK": {"S": "USER#user_alpha"},
                "SK": {"S": "PORTABLE#LIVE_STATE"},
            },
            "ProjectionExpression": (
                "PK, SK, #recordType, #userId, #generation, "
                "liveBundleHash, liveScheduleProjectionJson"
            ),
            "ExpressionAttributeNames": {
                "#recordType": "recordType",
                "#userId": "userId",
                "#generation": "generation",
            },
            "ConsistentRead": True,
        }
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(extra={"S": "x"}),
        lambda item: item["generation"].update(N="2"),
        lambda item: item["liveBundleHash"].update(S="b" * 64),
        lambda item: item["userId"].update(S="user_beta"),
        lambda item: item["liveScheduleProjectionJson"].update(
            S=item["liveScheduleProjectionJson"]["S"] + " "
        ),
    ],
)
def test_portable_projection_reader_rejects_malformed_or_unbound_state(mutate):
    client = MemorySchedulerClient()
    item = _portable_projection_item()
    mutate(item)
    client.items[("USER#user_alpha", "PORTABLE#LIVE_STATE")] = item
    reader = DynamoPortableScheduleProjectionReader(
        client=client,
        table_name="personal-operator-control",
    )

    with pytest.raises(RuntimeError, match="portable schedule projection"):
        reader.disabled_schedule_views("user_alpha")


def test_lists_only_strong_read_tenant_bound_schedule_views():
    client = MemorySchedulerClient()
    spec = _spec()
    client.seed_schedule(spec)

    result = _port(client).list_view("user_alpha")

    assert result == {
        "schedules": [
            {
                "scheduleId": spec.schedule_id,
                "taskType": "REMINDER",
                "state": "ENABLED",
                "nextRunAt": NOW + 3600,
            }
        ]
    }
    assert "IndexName" not in client.query_calls[0]
    assert client.query_calls[0]["ConsistentRead"] is True
    assert client.get_calls[0]["ConsistentRead"] is True


def test_native_definition_reader_exports_exact_tenant_specs_without_delivery_authority():
    client = MemorySchedulerClient()
    own = _spec()
    foreign = build_schedule_spec(
        schedule_id="schedule_87654321",
        user_id="user_beta",
        task_type="REMINDER",
        definition={
            "message": "foreign",
            "runAt": NOW + 7200,
            "timezone": "Europe/Tallinn",
        },
        revision=1,
        state="ENABLED",
    )
    client.seed_schedule(own)
    client.seed_schedule(foreign)

    definitions = DynamoScheduleDefinitionReader(
        client=client,
        table_name="personal-operator-scheduler-control",
    ).definitions_for_user("user_alpha")

    assert definitions == [own.to_mapping()]
    assert "deliveryTarget" not in definitions[0]
    assert "deliveryJson" not in definitions[0]
    assert "IndexName" not in client.query_calls[0]
    assert client.query_calls[0]["ConsistentRead"] is True
    assert all(call["ConsistentRead"] is True for call in client.get_calls)


def test_definition_reader_accepts_only_state_appropriate_terminal_ttl():
    client = MemorySchedulerClient()
    paused = build_schedule_spec(
        schedule_id="schedule_paused_12345678",
        user_id="user_alpha",
        task_type="REMINDER",
        definition={
            "message": "paused",
            "runAt": NOW + 3600,
            "timezone": "Europe/Tallinn",
        },
        revision=2,
        state="PAUSED",
    )
    client.seed_schedule(paused)
    reader = DynamoScheduleDefinitionReader(
        client=client,
        table_name="personal-operator-scheduler-control",
    )

    assert reader.definitions_for_user("user_alpha") == [paused.to_mapping()]

    client.items[(f"SCHEDULE#{paused.schedule_id}", "STATE")].pop("ttl")
    with pytest.raises(RuntimeError, match="scheduler control record"):
        reader.definitions_for_user("user_alpha")

    enabled = _spec()
    client = MemorySchedulerClient()
    client.seed_schedule(enabled)
    client.items[(f"SCHEDULE#{enabled.schedule_id}", "STATE")]["ttl"] = {
        "N": str(NOW + 90 * 86400)
    }
    with pytest.raises(RuntimeError, match="scheduler control record"):
        DynamoScheduleDefinitionReader(
            client=client,
            table_name="personal-operator-scheduler-control",
        ).definitions_for_user("user_alpha")


def test_definition_reader_skips_durable_owner_after_terminal_state_ttl_expiry():
    client = MemorySchedulerClient()
    client.seed_owner(
        user_id="user_alpha",
        schedule_id="schedule_aged_12345678",
    )
    reader = DynamoScheduleDefinitionReader(
        client=client,
        table_name="personal-operator-scheduler-control",
    )

    assert reader.definitions_for_user("user_alpha") == []
    assert _port(client).list_view("user_alpha") == {"schedules": []}
    assert all(call["ConsistentRead"] is True for call in client.get_calls)


def test_definition_reader_paginates_more_than_256_historical_owners():
    client = MemorySchedulerClient()
    for offset in range(300):
        client.seed_owner(
            user_id="user_alpha",
            schedule_id=f"schedule_history_{offset:08d}",
        )
    live = build_schedule_spec(
        schedule_id="schedule_z_live_12345678",
        user_id="user_alpha",
        task_type="REMINDER",
        definition={
            "message": "still live",
            "runAt": NOW + 7200,
            "timezone": "Europe/Tallinn",
        },
        revision=1,
        state="ENABLED",
    )
    client.seed_schedule(live)
    reader = DynamoScheduleDefinitionReader(
        client=client,
        table_name="personal-operator-scheduler-control",
    )

    assert reader.definitions_for_user("user_alpha") == [live.to_mapping()]
    assert len(client.query_calls) == 2
    assert "ExclusiveStartKey" not in client.query_calls[0]
    assert client.query_calls[1]["ExclusiveStartKey"]["PK"] == {
        "S": "USER#user_alpha"
    }


def test_schedule_list_merges_catalog_valid_imported_definitions_as_disabled_only():
    client = MemorySchedulerClient()
    native = _spec()
    client.seed_schedule(native)
    imported_spec = build_schedule_spec(
        schedule_id="schedule_imported_12345678",
        user_id="user_alpha",
        task_type="READ_ONLY_AGENT_TURN",
        definition={
            "prompt": "summarize workspace",
            "runAt": NOW + 10_800,
            "timezone": "Europe/Tallinn",
        },
        revision=4,
        state="PAUSED",
    ).to_mapping()
    imported_spec.pop("schema")
    imported_spec.pop("nextRunAt")
    imported_spec["state"] = "DISABLED"

    class Imported:
        def disabled_schedules(self, user_id):
            assert user_id == "user_alpha"
            return [imported_spec]

    port = DynamoScheduleCapabilityPort(
        client=client,
        table_name="personal-operator-scheduler-control",
        authority_table_name="personal-operator-capability-state",
        catalog_digest="c" * 64,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce_12345678",
        imported_schedules=Imported(),
    )

    assert port.list_view("user_alpha") == {
        "schedules": [
            {
                "scheduleId": "schedule_12345678",
                "taskType": "REMINDER",
                "state": "ENABLED",
                "nextRunAt": NOW + 3600,
            },
            {
                "scheduleId": "schedule_imported_12345678",
                "taskType": "READ_ONLY_AGENT_TURN",
                "state": "PAUSED",
                "nextRunAt": None,
            },
        ]
    }


def test_imported_schedule_projection_rejects_cross_tenant_or_armed_rows():
    client = MemorySchedulerClient()
    imported = _spec(user_id="user_beta").to_mapping()
    imported.pop("schema")
    imported["state"] = "DISABLED"
    imported.pop("nextRunAt")

    class Imported:
        def disabled_schedules(self, _user_id):
            return [imported]

    port = DynamoScheduleCapabilityPort(
        client=client,
        table_name="personal-operator-scheduler-control",
        authority_table_name="personal-operator-capability-state",
        catalog_digest="c" * 64,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce_12345678",
        imported_schedules=Imported(),
    )

    with pytest.raises(RuntimeError, match="imported schedule"):
        port.list_view("user_alpha")

    imported["userId"] = "user_alpha"
    imported["nextRunAt"] = NOW + 3600
    with pytest.raises(RuntimeError, match="imported schedule"):
        port.list_view("user_alpha")


def test_proposals_are_durable_bounded_and_never_dispatch_a_live_schedule():
    client = MemorySchedulerClient()
    client.seed_delivery()
    port = _port(client)

    proposal = port.propose(
        user_id="user_alpha",
        invocation_id="invocation_12345678",
        task_type="REMINDER",
        definition={
            "message": "review notes",
            "runAt": NOW + 3600,
            "timezone": "Europe/Tallinn",
        },
    )

    assert set(proposal) == {"proposalRef", "expiresAt"}
    assert proposal["expiresAt"] == NOW + 900
    item = next(
        item
        for (_pk, sk), item in client.items.items()
        if sk.startswith("PROPOSAL#")
    )
    record = json.loads(item["recordJson"]["S"])
    assert record["proposal"]["operationId"] == "schedule.propose"
    assert record["proposal"]["userId"] == "user_alpha"
    assert record["deliveryTarget"] == {
        "actorId": "telegram:1",
        "chatId": "1",
    }
    assert client.get_calls[0]["Key"] == {
        "PK": {"S": subject_partition_key("user_alpha")},
        "SK": {"S": "DELIVERY#invocation_12345678"},
    }
    assert client.put_calls[0]["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )


def test_proposal_fails_closed_without_exact_current_turn_delivery_context():
    client = MemorySchedulerClient()

    with pytest.raises(RuntimeError, match="delivery"):
        _port(client).propose(
            user_id="user_alpha",
            invocation_id="invocation_12345678",
            task_type="REMINDER",
            definition={
                "message": "review notes",
                "runAt": NOW + 3600,
                "timezone": "Europe/Tallinn",
            },
        )

    assert client.put_calls == []


def test_expiring_proposals_never_enter_or_poison_the_schedule_listing_index():
    client = MemorySchedulerClient()
    spec = _spec()
    client.seed_schedule(spec)
    client.seed_delivery()
    counter = itertools.count()
    port = _port(
        client,
        nonce_factory=lambda: f"nonce_{next(counter):08d}",
    )

    for _ in range(257):
        port.propose(
            user_id="user_alpha",
            invocation_id="invocation_12345678",
            task_type="REMINDER",
            definition={
                "message": "review notes",
                "runAt": NOW + 3600,
                "timezone": "Europe/Tallinn",
            },
        )

    assert port.list_view("user_alpha")["schedules"] == [
        {
            "scheduleId": spec.schedule_id,
            "taskType": "REMINDER",
            "state": "ENABLED",
            "nextRunAt": NOW + 3600,
        }
    ]
    proposals = [
        item
        for (_pk, sk), item in client.items.items()
        if sk.startswith("PROPOSAL#")
    ]
    assert len(proposals) == 257
    assert all("scheduleUserId" not in item for item in proposals)
    assert all(item["state"] == {"S": "PENDING"} for item in proposals)
    assert all(item["ttl"] == {"N": str(NOW + 90 * 86400)} for item in proposals)


def test_cancel_proposal_rejects_a_foreign_schedule_before_any_write():
    client = MemorySchedulerClient()
    client.seed_schedule(_spec(user_id="user_beta"))
    client.seed_delivery()

    with pytest.raises(RuntimeError, match="schedule"):
        _port(client).cancel_propose(
            user_id="user_alpha",
            invocation_id="invocation_12345678",
            schedule_id="schedule_12345678",
        )

    assert client.put_calls == []
