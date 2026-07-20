from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from capabilities.catalog import compile_catalog
from scheduler.proposals import (
    ScheduleProposalRecordV1,
    build_cancel_schedule_proposal,
    build_create_schedule_proposal,
)


NOW = 1_800_000_000
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "specs/capabilities/schemas"
CATALOG = compile_catalog("a" * 40, SCHEMA_DIR)[1]
DELIVERY = {"actorId": "telegram:42", "chatId": "42"}
DEFINITION = {
    "message": "review notes",
    "runAt": NOW + 3600,
    "timezone": "Europe/Tallinn",
}


def _create():
    return build_create_schedule_proposal(
        catalog_digest=CATALOG.catalog_digest,
        user_id="user_alpha",
        invocation_id="invocation_12345678",
        task_type="REMINDER",
        definition=DEFINITION,
        delivery_target=DELIVERY,
        now=NOW,
        nonce="nonce_12345678",
    )


def test_create_and_cancel_round_trip_one_shared_action_proposal_contract():
    created = _create()
    cancelled = build_cancel_schedule_proposal(
        catalog_digest=CATALOG.catalog_digest,
        user_id="user_alpha",
        invocation_id="invocation_87654321",
        schedule_id=created.schedule_id,
        revision=3,
        delivery_target=DELIVERY,
        now=NOW,
        nonce="nonce_87654321",
    )

    assert ScheduleProposalRecordV1.from_mapping(created.to_mapping()) == created
    assert ScheduleProposalRecordV1.from_mapping(cancelled.to_mapping()) == cancelled
    assert created.proposal.operation_id == "schedule.propose"
    assert created.proposal.args_hash == created.args_hash
    assert created.proposal.revision == 1
    assert cancelled.proposal.operation_id == "schedule.cancel.propose"
    assert cancelled.proposal.arguments == {"scheduleId": created.schedule_id}
    assert cancelled.proposal.revision == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["proposal"].update(catalogDigest="f" * 64),
        lambda value: value["proposal"].update(toolName="po_schedule_list"),
        lambda value: value["proposal"].update(resource="schedule:other_12345678"),
        lambda value: value["proposal"].update(argsHash="f" * 64),
        lambda value: value["proposal"].update(revision=2),
        lambda value: value["proposal"].update(
            originatingInvocationId="invocation_87654321"
        ),
        lambda value: value.update(
            deliveryTarget={"actorId": "telegram:43", "chatId": "42"}
        ),
        lambda value: value.update(scheduleId="schedule_other_1234"),
    ],
)
def test_shared_record_rejects_any_catalog_call_revision_or_delivery_substitution(
    mutate,
):
    value = deepcopy(_create().to_mapping())
    mutate(value)

    with pytest.raises(Exception):
        ScheduleProposalRecordV1.from_mapping(value)
