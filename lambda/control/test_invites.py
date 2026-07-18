from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from threading import Lock

import pytest
from tests.provider_test_support import ClientError

from .invites import (
    DynamoPilotInvites,
    InviteRejected,
    InviteStoreError,
)


def conditional_failure(operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "synthetic conditional failure",
            }
        },
        operation,
    )


def test_invite_failures_use_the_real_botocore_client_error() -> None:
    assert ClientError.__module__ == "botocore.exceptions"
    assert issubclass(ClientError, Exception)


def transaction_cancelled(
    count: int,
    conditional_index: int | None = None,
    *,
    reason_code: str | None = None,
) -> ClientError:
    reasons = [{"Code": "None"} for _ in range(count)]
    if conditional_index is not None:
        reasons[conditional_index] = {
            "Code": reason_code or "ConditionalCheckFailed",
            "Message": "synthetic cancellation",
        }
    return ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "synthetic transaction cancellation",
            },
            "CancellationReasons": reasons,
        },
        "TransactWriteItems",
    )


def _decode(value):
    if "S" in value:
        return value["S"]
    if "N" in value:
        return int(value["N"])
    raise AssertionError(f"unexpected Dynamo value: {value!r}")


def _item(value):
    return {name: _decode(field) for name, field in value.items()}


class MemoryInviteTable:
    name = "openclaw-identity"

    def __init__(self):
        self.items = {}
        self.calls = []
        self.lock = Lock()
        self.meta = type("Meta", (), {"client": self})()
        self.raise_before_transaction = False
        self.raise_after_transaction = False
        self.transaction_reason_code = None

    @staticmethod
    def _key(value):
        return value["PK"], value["SK"]

    def put_item(self, **request):
        self.calls.append(("put_item", deepcopy(request)))
        item = deepcopy(request["Item"])
        key = self._key(item)
        with self.lock:
            if key in self.items:
                raise conditional_failure("PutItem")
            self.items[key] = item
        return {}

    def get_item(self, **request):
        self.calls.append(("get_item", deepcopy(request)))
        with self.lock:
            found = self.items.get(self._key(request["Key"]))
            return {} if found is None else {"Item": deepcopy(found)}

    def update_item(self, **request):
        self.calls.append(("update_item", deepcopy(request)))
        values = request["ExpressionAttributeValues"]
        key = self._key(request["Key"])
        with self.lock:
            current = self.items.get(key)
            if (
                current is None
                or current.get("recordType") != values[":recordType"]
                or current.get("status") != values[":issued"]
                or current.get("ttl", 0) <= values[":now"]
            ):
                raise conditional_failure("UpdateItem")
            current["status"] = values[":revoked"]
            current["revokedAt"] = values[":now"]
            return {"Attributes": deepcopy(current)}

    def transact_write_items(self, **request):
        self.calls.append(("transact_write_items", deepcopy(request)))
        if self.raise_before_transaction:
            self.raise_before_transaction = False
            raise TimeoutError("synthetic transaction outage")
        if self.transaction_reason_code is not None:
            reason = self.transaction_reason_code
            self.transaction_reason_code = None
            raise transaction_cancelled(
                len(request["TransactItems"]),
                0,
                reason_code=reason,
            )
        with self.lock:
            pending = deepcopy(self.items)
            for index, operation in enumerate(request["TransactItems"]):
                if "Update" in operation:
                    update = operation["Update"]
                    key = self._key(_item(update["Key"]))
                    values = {
                        name: _decode(value)
                        for name, value in update["ExpressionAttributeValues"].items()
                    }
                    current = pending.get(key)
                    if (
                        current is None
                        or current.get("recordType") != values[":recordType"]
                        or current.get("status") != values[":issued"]
                        or current.get("ttl", 0) <= values[":now"]
                        or "redeemedActorDigest" in current
                    ):
                        raise transaction_cancelled(
                            len(request["TransactItems"]), index
                        )
                    current.update(
                        status=values[":redeemed"],
                        redeemedAt=values[":now"],
                        redeemedActorDigest=values[":actorDigest"],
                        userId=values[":userId"],
                    )
                elif "ConditionCheck" in operation:
                    check = operation["ConditionCheck"]
                    key = self._key(_item(check["Key"]))
                    if key in pending:
                        raise transaction_cancelled(
                            len(request["TransactItems"]), index
                        )
                elif "Put" in operation:
                    put = operation["Put"]
                    item = _item(put["Item"])
                    key = self._key(item)
                    if key in pending:
                        raise transaction_cancelled(
                            len(request["TransactItems"]), index
                        )
                    pending[key] = item
                else:
                    raise AssertionError(f"unexpected transaction: {operation!r}")
            self.items = pending
            if self.raise_after_transaction:
                self.raise_after_transaction = False
                raise TimeoutError("synthetic response loss")
        return {}


@dataclass
class Clock:
    value: int = 1_800_000_000

    def __call__(self):
        return self.value


class RandomBytes:
    def __init__(self, byte: int = 7):
        self.byte = byte

    def __call__(self, length: int) -> bytes:
        assert length == 24
        return bytes([self.byte]) * length


def service(*, table=None, clock=None, random=None):
    return DynamoPilotInvites(
        table or MemoryInviteTable(),
        now=clock or Clock(),
        random_bytes=random or RandomBytes(),
    )


def test_issue_returns_exact_opaque_form_and_persists_digest_only(caplog):
    table = MemoryInviteTable()
    invites = service(table=table)

    issued = invites.issue(ttl_seconds=3_600)

    assert issued.token.startswith("poi1_")
    assert len(issued.token) == 37
    assert base64.urlsafe_b64decode(issued.token[5:] + "==") == bytes([7]) * 24
    serialized = json.dumps(list(table.items.values()), sort_keys=True)
    assert issued.token not in serialized
    assert issued.token not in caplog.text
    digest = hashlib.sha256(issued.token.encode("ascii")).hexdigest()
    assert table.items[(f"PILOT_INVITE#{digest}", "INVITE")] == {
        "PK": f"PILOT_INVITE#{digest}",
        "SK": "INVITE",
        "recordType": "PILOT_INVITE_V1",
        "status": "ISSUED",
        "issuedAt": 1_800_000_000,
        "ttl": 1_800_003_600,
    }


@pytest.mark.parametrize(
    "token",
    [None, "", "poi1_short", "poi2_" + "A" * 32, "poi1_" + "+" * 32],
)
def test_malformed_tokens_are_rejected_without_storage_access(token):
    table = MemoryInviteTable()

    with pytest.raises(InviteRejected, match="unavailable"):
        service(table=table).redeem(
            token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )

    assert table.calls == []


def test_atomic_redeem_creates_exact_identity_and_same_actor_retry_is_idempotent():
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token

    first = invites.redeem(
        token,
        channel="telegram",
        channel_user_id="42",
        display_name="Ada",
    )
    replay = invites.redeem(
        token,
        channel="telegram",
        channel_user_id="42",
        display_name="Ada changed",
    )

    expected_user = "user_" + hashlib.sha256(b"telegram:42").hexdigest()[:16]
    assert first.user_id == expected_user
    assert first.created is True
    assert replay.user_id == expected_user
    assert replay.created is False
    assert table.items[("CHANNEL#telegram:42", "PROFILE")]["userId"] == expected_user
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    record = table.items[(f"PILOT_INVITE#{digest}", "INVITE")]
    assert record["status"] == "REDEEMED"
    assert record["userId"] == expected_user
    assert token not in json.dumps(list(table.items.values()), sort_keys=True)


def test_redeem_race_has_one_winner_and_cross_actor_replays_are_denied():
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token

    def redeem(actor):
        try:
            return invites.redeem(
                token,
                channel="telegram",
                channel_user_id=str(actor),
                display_name=f"Pilot {actor}",
            ).user_id
        except InviteRejected:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(redeem, range(40, 48)))

    assert len([result for result in results if result is not None]) == 1
    with pytest.raises(InviteRejected, match="unavailable"):
        invites.redeem(
            token,
            channel="telegram",
            channel_user_id="99",
            display_name="Mallory",
        )


def test_expired_revoked_unknown_and_deleted_actor_invites_fail_closed():
    clock = Clock()

    expired_table = MemoryInviteTable()
    expired = service(table=expired_table, clock=clock)
    expired_token = expired.issue(ttl_seconds=60).token
    clock.value += 60
    with pytest.raises(InviteRejected, match="unavailable"):
        expired.redeem(
            expired_token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )

    clock.value -= 60
    revoked_table = MemoryInviteTable()
    revoked = service(table=revoked_table, clock=clock, random=RandomBytes(8))
    revoked_token = revoked.issue().token
    assert revoked.revoke(revoked_token) is True
    assert revoked.revoke(revoked_token) is True
    with pytest.raises(InviteRejected, match="unavailable"):
        revoked.redeem(
            revoked_token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )

    with pytest.raises(InviteRejected, match="unavailable"):
        revoked.redeem(
            "poi1_" + "A" * 32,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )

    deleted_table = MemoryInviteTable()
    deleted = service(table=deleted_table, clock=clock, random=RandomBytes(9))
    deleted_token = deleted.issue().token
    channel_key = "telegram:42"
    tombstone = "CHANNEL_TOMBSTONE#" + hashlib.sha256(
        channel_key.encode("utf-8")
    ).hexdigest()
    deleted_table.items[(tombstone, "TOMBSTONE")] = {
        "PK": tombstone,
        "SK": "TOMBSTONE",
    }
    with pytest.raises(InviteRejected, match="unavailable"):
        deleted.redeem(
            deleted_token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )


def test_ambiguous_transaction_reconciles_only_the_exact_actor_binding():
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token
    table.raise_after_transaction = True

    result = invites.redeem(
        token,
        channel="telegram",
        channel_user_id="42",
        display_name="Ada",
    )

    assert result.created is True
    assert result.user_id.startswith("user_")


def test_transaction_outage_before_commit_is_retryable_not_an_invite_rejection():
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token
    table.raise_before_transaction = True

    with pytest.raises(InviteStoreError, match="redemption"):
        invites.redeem(
            token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )

    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    assert table.items[(f"PILOT_INVITE#{digest}", "INVITE")]["status"] == "ISSUED"


@pytest.mark.parametrize(
    "reason_code",
    [
        "ProvisionedThroughputExceeded",
        "ThrottlingError",
        "TransactionConflict",
        "InternalServerError",
    ],
)
def test_nonconditional_transaction_cancellation_is_retryable(reason_code):
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token
    table.transaction_reason_code = reason_code

    with pytest.raises(InviteStoreError, match="redemption"):
        invites.redeem(
            token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )


def test_transaction_cancellation_without_exact_reasons_is_retryable():
    class MissingReasons(MemoryInviteTable):
        def transact_write_items(self, **request):
            self.calls.append(("transact_write_items", deepcopy(request)))
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "missing cancellation reasons",
                    }
                },
                "TransactWriteItems",
            )

    invites = service(table=MissingReasons())
    token = invites.issue().token

    with pytest.raises(InviteStoreError, match="redemption"):
        invites.redeem(
            token,
            channel="telegram",
            channel_user_id="42",
            display_name="Ada",
        )


def test_invalid_issue_randomness_and_collision_never_reissues_plaintext():
    table = MemoryInviteTable()
    invites = service(table=table)
    token = invites.issue().token

    with pytest.raises(InviteStoreError, match="collision"):
        invites.issue()
    assert token not in repr(table.calls)


def test_operator_cli_issues_once_and_revokes_without_a_provider_call():
    script = Path(__file__).resolve().parents[2] / "scripts" / "pilot-invites.py"
    spec = importlib.util.spec_from_file_location("pilot_invites_cli", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    table = MemoryInviteTable()
    stdout = io.StringIO()
    factory_calls = []

    def table_factory(*, table_name, region):
        factory_calls.append((table_name, region))
        return table

    assert module.main(
        ["issue", "--table", "openclaw-identity", "--ttl-seconds", "3600"],
        table_factory=table_factory,
        stdout=stdout,
        now=Clock(),
        random_bytes=RandomBytes(10),
    ) == 0
    token = stdout.getvalue().strip()
    assert token.startswith("poi1_")
    stdout.seek(0)
    stdout.truncate(0)

    assert module.main(
        ["revoke", "--table", "openclaw-identity", "--token", token],
        table_factory=table_factory,
        stdout=stdout,
        now=Clock(),
    ) == 0
    assert stdout.getvalue() == "revoked\n"
    assert factory_calls == [
        ("openclaw-identity", "eu-west-1"),
        ("openclaw-identity", "eu-west-1"),
    ]
