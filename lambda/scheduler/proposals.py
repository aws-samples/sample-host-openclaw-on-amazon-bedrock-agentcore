"""One immutable schedule CREATE/CANCEL proposal contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Any, Mapping

from capabilities.contracts import (
    ActionProposalV1,
    canonical_json_bytes,
    canonical_sha256,
)
from scheduler.models import build_schedule_spec, derive_schedule_id


SCHEMA = "personal-operator.schedule-proposal-record.v1"
PROPOSAL_TTL_SECONDS = 15 * 60
PHYSICAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_CHAT_ID = re.compile(r"[1-9][0-9]{0,19}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _delivery(value: Mapping[str, Any]) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"actorId", "chatId"}
        or not isinstance(value.get("chatId"), str)
        or _CHAT_ID.fullmatch(value["chatId"]) is None
        or value.get("actorId") != f"telegram:{value['chatId']}"
    ):
        raise ValueError("schedule proposal delivery target is invalid")
    return {"actorId": value["actorId"], "chatId": value["chatId"]}


def _proposal_id(binding: Mapping[str, Any], nonce: str) -> str:
    if not isinstance(nonce, str) or _OPAQUE_ID.fullmatch(nonce) is None:
        raise ValueError("schedule proposal nonce is invalid")
    digest = hashlib.sha256(b"personal-operator.schedule-proposal-record.v1\0")
    digest.update(canonical_json_bytes(binding))
    digest.update(b"\0")
    digest.update(nonce.encode("utf-8"))
    return f"proposal_{digest.hexdigest()}"


def _binding_hash(
    proposal: ActionProposalV1,
    schedule_id: str,
    delivery_target: Mapping[str, str],
    created_at: int,
) -> str:
    return canonical_sha256(
        {
            "proposal": proposal.to_mapping(),
            "scheduleId": schedule_id,
            "deliveryTarget": dict(delivery_target),
            "createdAt": created_at,
        }
    )


@dataclass(frozen=True, slots=True)
class ScheduleProposalRecordV1:
    proposal: ActionProposalV1
    schedule_id: str
    delivery_target: Mapping[str, str]
    created_at: int
    binding_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposalV1):
            raise TypeError("schedule proposal requires ActionProposalV1")
        if (
            not isinstance(self.schedule_id, str)
            or _OPAQUE_ID.fullmatch(self.schedule_id) is None
        ):
            raise ValueError("schedule proposal scheduleId is invalid")
        delivery = _delivery(self.delivery_target)
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, int)
            or self.created_at < 0
            or self.proposal.expires_at <= self.created_at
            or self.proposal.expires_at - self.created_at > PROPOSAL_TTL_SECONDS
        ):
            raise ValueError("schedule proposal lifetime is invalid")
        if self.proposal.operation_id == "schedule.propose":
            if self.proposal.revision != 1:
                raise ValueError("schedule create proposal revision is invalid")
            spec = build_schedule_spec(
                schedule_id=self.schedule_id,
                user_id=self.proposal.user_id,
                task_type=self.proposal.arguments["taskType"],
                definition=self.proposal.arguments["definition"],
                revision=1,
                state="PAUSED",
                next_run_at=None,
            )
            if spec.user_id != self.proposal.user_id:
                raise ValueError("schedule proposal user binding is invalid")
        elif self.proposal.operation_id == "schedule.cancel.propose":
            if self.proposal.arguments["scheduleId"] != self.schedule_id:
                raise ValueError("schedule cancellation target is invalid")
        else:
            raise ValueError("schedule proposal operation is invalid")
        expected = _binding_hash(
            self.proposal,
            self.schedule_id,
            delivery,
            self.created_at,
        )
        if (
            not isinstance(self.binding_hash, str)
            or _SHA256.fullmatch(self.binding_hash) is None
            or self.binding_hash != expected
        ):
            raise ValueError("schedule proposal record binding is invalid")
        object.__setattr__(self, "delivery_target", MappingProxyType(delivery))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScheduleProposalRecordV1":
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "proposal",
            "scheduleId",
            "deliveryTarget",
            "createdAt",
            "bindingHash",
        }:
            raise ValueError("schedule proposal record shape is invalid")
        if value.get("schema") != SCHEMA:
            raise ValueError("schedule proposal record schema is invalid")
        return cls(
            proposal=ActionProposalV1.from_mapping(value["proposal"]),
            schedule_id=value["scheduleId"],
            delivery_target=value["deliveryTarget"],
            created_at=value["createdAt"],
            binding_hash=value["bindingHash"],
        )

    @property
    def proposal_id(self) -> str:
        return self.proposal.proposal_id

    @property
    def user_id(self) -> str:
        return self.proposal.user_id

    @property
    def args_hash(self) -> str:
        return self.proposal.args_hash

    @property
    def expires_at(self) -> int:
        return self.proposal.expires_at

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "proposal": self.proposal.to_mapping(),
            "scheduleId": self.schedule_id,
            "deliveryTarget": dict(self.delivery_target),
            "createdAt": self.created_at,
            "bindingHash": self.binding_hash,
        }


def _record(
    *,
    catalog_digest: str,
    user_id: str,
    invocation_id: str,
    operation_id: str,
    tool_name: str,
    resource: str,
    arguments: Mapping[str, Any],
    revision: int,
    schedule_id: str,
    delivery_target: Mapping[str, Any],
    now: int,
    nonce: str,
) -> ScheduleProposalRecordV1:
    delivery = _delivery(delivery_target)
    args_hash = canonical_sha256(arguments)
    binding = {
        "catalogDigest": catalog_digest,
        "userId": user_id,
        "invocationId": invocation_id,
        "operationId": operation_id,
        "resource": resource,
        "arguments": dict(arguments),
        "revision": revision,
        "scheduleId": schedule_id,
        "deliveryTarget": delivery,
        "createdAt": now,
    }
    proposal = ActionProposalV1.from_mapping(
        {
            "schema": ActionProposalV1.SCHEMA,
            "proposalId": _proposal_id(binding, nonce),
            "userId": user_id,
            "catalogDigest": catalog_digest,
            "connectorSchemaDigest": None,
            "operationId": operation_id,
            "toolName": tool_name,
            "capabilityId": operation_id,
            "resource": resource,
            "connectionRef": None,
            "arguments": dict(arguments),
            "argsHash": args_hash,
            "revision": revision,
            "originatingInvocationId": invocation_id,
            "approvalPolicy": "EXACT_ONE_TIME",
            "expiresAt": now + PROPOSAL_TTL_SECONDS,
        }
    )
    return ScheduleProposalRecordV1(
        proposal=proposal,
        schedule_id=schedule_id,
        delivery_target=delivery,
        created_at=now,
        binding_hash=_binding_hash(proposal, schedule_id, delivery, now),
    )


def build_create_schedule_proposal(
    *,
    catalog_digest: str,
    user_id: str,
    invocation_id: str,
    task_type: str,
    definition: Mapping[str, Any],
    delivery_target: Mapping[str, Any],
    now: int,
    nonce: str,
) -> ScheduleProposalRecordV1:
    schedule_id = derive_schedule_id(user_id, nonce)
    arguments = {"taskType": task_type, "definition": dict(definition)}
    return _record(
        catalog_digest=catalog_digest,
        user_id=user_id,
        invocation_id=invocation_id,
        operation_id="schedule.propose",
        tool_name="po_schedule_propose",
        resource="schedule:new",
        arguments=arguments,
        revision=1,
        schedule_id=schedule_id,
        delivery_target=delivery_target,
        now=now,
        nonce=nonce,
    )


def build_cancel_schedule_proposal(
    *,
    catalog_digest: str,
    user_id: str,
    invocation_id: str,
    schedule_id: str,
    revision: int,
    delivery_target: Mapping[str, Any],
    now: int,
    nonce: str,
) -> ScheduleProposalRecordV1:
    return _record(
        catalog_digest=catalog_digest,
        user_id=user_id,
        invocation_id=invocation_id,
        operation_id="schedule.cancel.propose",
        tool_name="po_schedule_cancel_propose",
        resource=f"schedule:{schedule_id}",
        arguments={"scheduleId": schedule_id},
        revision=revision,
        schedule_id=schedule_id,
        delivery_target=delivery_target,
        now=now,
        nonce=nonce,
    )


__all__ = [
    "PHYSICAL_RETENTION_SECONDS",
    "PROPOSAL_TTL_SECONDS",
    "ScheduleProposalRecordV1",
    "build_cancel_schedule_proposal",
    "build_create_schedule_proposal",
]
