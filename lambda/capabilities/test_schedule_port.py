from __future__ import annotations

import itertools
import json

import pytest

from capabilities.schedule_port import DynamoScheduleCapabilityPort
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
        user_id = kwargs["ExpressionAttributeValues"][":user"]["S"]
        index_field = kwargs["ExpressionAttributeNames"]["#user"]
        items = []
        for (pk, sk), item in self.items.items():
            if item.get(index_field, {}).get("S") == user_id:
                items.append(
                    {
                        "PK": {"S": pk},
                        "SK": {"S": sk},
                        index_field: {"S": user_id},
                        "scheduleSortKey": item["scheduleSortKey"],
                    }
                )
        return {"Items": items}

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
        self.items[(f"SCHEDULE#{spec.schedule_id}", "STATE")] = {
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

    def seed_delivery(self, *, user_id="user_alpha", invocation_id="invocation_12345678"):
        self.items[(f"TURN#{invocation_id}", "DELIVERY")] = {
            "PK": {"S": f"TURN#{invocation_id}"},
            "SK": {"S": "DELIVERY"},
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
    assert client.query_calls[0]["IndexName"] == "schedule-user-index-v1"
    assert client.get_calls[0]["ConsistentRead"] is True


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
