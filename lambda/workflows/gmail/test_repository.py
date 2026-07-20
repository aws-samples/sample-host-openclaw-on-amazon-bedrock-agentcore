from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest


GMAIL_DIR = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, GMAIL_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models = _load("gmail_models", "models.py")
repository_module = _load("gmail_repository", "repository.py")

NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
DERIVED_TTL = int((NOW + timedelta(days=14)).timestamp())


class ConditionalFailure(Exception):
    pass


class AmbiguousWrite(Exception):
    pass


class FakeTable:
    name = "gmail-table"

    def __init__(self):
        self.items = {}
        self.calls = []
        self.meta = SimpleNamespace(client=self)

    @staticmethod
    def _key(item):
        return item["PK"], item["SK"]

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        key = self._key(kwargs["Item"])
        if kwargs.get("ConditionExpression") and key in self.items:
            raise ConditionalFailure("conditional write rejected")
        self.items[key] = kwargs["Item"]
        return {}

    def delete_item(self, **kwargs):
        self.calls.append(("delete_item", kwargs))
        key = self._key(kwargs["Key"])
        old = self.items.get(key)
        expected = kwargs.get("ExpressionAttributeValues", {}).get(":generation")
        if expected is not None and (
            old is None or old.get("connectionGeneration") != expected
        ):
            raise ConditionalFailure("generation changed")
        old = self.items.pop(key, None)
        return {"Attributes": old} if old is not None else {}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        key = self._key(kwargs["Key"])
        item = self.items.get(key)
        values = kwargs["ExpressionAttributeValues"]
        expected_statuses = {
            value
            for name, value in values.items()
            if name.startswith(":expectedStatus")
        }
        if (
            item is None
            or item.get("generation") != values[":expected"]
            or (
                expected_statuses
                and item.get("status") not in expected_statuses
            )
        ):
            raise ConditionalFailure("generation changed")
        item["generation"] = values[":next"]
        item["status"] = values[":status"]
        item["updatedAt"] = values[":now"]
        return {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": item} if item is not None else {}

    @classmethod
    def _decode_value(cls, value):
        if "S" in value:
            return value["S"]
        if "N" in value:
            number = Decimal(value["N"])
            return int(number) if number == number.to_integral_value() else number
        if "BOOL" in value:
            return value["BOOL"]
        if "NULL" in value:
            return None
        if "M" in value:
            return {
                name: cls._decode_value(field)
                for name, field in value["M"].items()
            }
        if "L" in value:
            return [cls._decode_value(field) for field in value["L"]]
        raise AssertionError(f"unsupported Dynamo value: {value!r}")

    @classmethod
    def _decode_item(cls, item):
        return {name: cls._decode_value(field) for name, field in item.items()}

    def _before_transaction(self, _operations):
        return None

    def transact_write_items(self, **kwargs):
        operations = kwargs["TransactItems"]
        self.calls.append(("transact_write_items", kwargs))
        self._before_transaction(operations)
        pending = {key: dict(item) for key, item in self.items.items()}
        for operation in operations:
            if "ConditionCheck" in operation:
                check = operation["ConditionCheck"]
                key = self._key(self._decode_item(check["Key"]))
                item = pending.get(key)
                expression = check["ConditionExpression"]
                values = {
                    name: self._decode_value(value)
                    for name, value in check.get(
                        "ExpressionAttributeValues", {}
                    ).items()
                }
                if expression.startswith("attribute_not_exists"):
                    valid = item is None
                elif expression == "connectionGeneration=:generation":
                    valid = (
                        item is not None
                        and item.get("connectionGeneration")
                        == values[":generation"]
                    )
                elif expression == "generation=:generation AND #status=:status":
                    valid = (
                        item is not None
                        and item.get("generation") == values[":generation"]
                        and item.get("status") == values[":status"]
                    )
                else:
                    valid = (
                        item is not None
                        and item.get("generation") == values[":generation"]
                        and item.get("status")
                        in {
                            value
                            for name, value in values.items()
                            if name.startswith(":status")
                        }
                    )
                if not valid:
                    raise ConditionalFailure("transaction condition rejected")
            elif "Put" in operation:
                put = operation["Put"]
                item = self._decode_item(put["Item"])
                key = self._key(item)
                if put.get("ConditionExpression") and key in pending:
                    raise ConditionalFailure("transaction put rejected")
                pending[key] = item
            elif "Update" in operation:
                update = operation["Update"]
                key = self._key(self._decode_item(update["Key"]))
                item = pending.get(key)
                values = {
                    name: self._decode_value(value)
                    for name, value in update[
                        "ExpressionAttributeValues"
                    ].items()
                }
                if ":expectedState" in values:
                    if (
                        item is None
                        or item.get("actionId") != values[":actionId"]
                        or item.get("userId") != values[":userId"]
                        or item.get("state") != values[":expectedState"]
                        or item.get("revision") != values[":expectedRevision"]
                        or item.get("draftRevision")
                        != values[":actionDraftRevision"]
                        or item.get("ttl", 0) <= values[":nowEpoch"]
                        or (
                            values[":expectedState"] == "STALE"
                            and (
                                item.get("staleReason")
                                != values[":staleReason"]
                                or item.get("staleDraftRevision")
                                != values[":staleDraftRevision"]
                                or item.get("supersededByDraftRevision")
                                != values[":expectedDraftRevision"]
                            )
                        )
                    ):
                        raise ConditionalFailure(
                            "action transition rejected"
                        )
                    item.update(
                        {
                            "state": values[":staleState"],
                            "revision": values[":nextRevision"],
                            "updatedAt": values[":updatedAt"],
                            "lastTransitionId": values[":transitionId"],
                            "staleAt": values[":updatedAt"],
                            "staleReason": values[":staleReason"],
                            "staleDraftRevision": values[
                                ":staleDraftRevision"
                            ],
                            "supersededByDraftRevision": values[
                                ":currentDraftRevision"
                            ],
                            "ttl": values[":retentionTtl"],
                        }
                    )
                elif (
                    item is None
                    or item.get("generation") != values[":generation"]
                    or item.get("status")
                    not in {values[":disconnected"], values[":connected"]}
                ):
                    raise ConditionalFailure("transaction update rejected")
                else:
                    item["status"] = values[":connected"]
                    item["updatedAt"] = values[":now"]
            elif "Delete" in operation:
                delete = operation["Delete"]
                key = self._key(self._decode_item(delete["Key"]))
                pending.pop(key, None)
            else:
                raise AssertionError(f"unsupported transaction: {operation!r}")
        self.items = pending
        return {}


def repository(table=None):
    table = table or FakeTable()
    return repository_module.DynamoGmailRepository(
        table,
        conditional_failure_types=(ConditionalFailure,),
        now=lambda: NOW,
    ), table


def test_oauth_state_is_conditionally_created_and_atomically_consumed_once():
    repo, table = repository()
    expires_at = int((NOW + timedelta(minutes=10)).timestamp())
    value = {
        "user_id": "user-1",
        "redirect_uri": "https://app.example/callback",
        "code_verifier": "verifier-secret",
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }

    repo.put_once("a" * 64, value, expires_at=expires_at)

    item = table.items[("OAUTH_STATE#" + "a" * 64, "OAUTH_STATE")]
    assert item == {
        "PK": "OAUTH_STATE#" + "a" * 64,
        "SK": "OAUTH_STATE",
        "userId": "user-1",
        "state": value,
        "ttl": expires_at,
    }
    put_call = table.calls[0][1]
    assert put_call["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )
    with pytest.raises(repository_module.DuplicateOAuthStateError):
        repo.put_once("a" * 64, value, expires_at=expires_at)

    assert repo.pop_once("a" * 64) == value
    assert repo.pop_once("a" * 64) is None
    delete_call = next(call for name, call in table.calls if name == "delete_item")
    assert delete_call["ReturnValues"] == "ALL_OLD"


def test_expired_oauth_state_is_consumed_but_never_returned_before_dynamodb_ttl_cleanup():
    repo, table = repository()
    key = "e" * 64
    expired_at = int((NOW - timedelta(seconds=1)).timestamp())
    table.items[(f"OAUTH_STATE#{key}", "OAUTH_STATE")] = {
        "PK": f"OAUTH_STATE#{key}",
        "SK": "OAUTH_STATE",
        "state": {
            "user_id": "user-1",
            "redirect_uri": "https://app.example/callback",
            "code_verifier": "verifier-secret",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
        "ttl": expired_at,
    }

    assert repo.pop_once(key) is None
    assert (f"OAUTH_STATE#{key}", "OAUTH_STATE") not in table.items


def test_token_store_persists_only_an_envelope_without_derived_ttl():
    repo, table = repository()
    envelope = {
        "format": "personal-operator.oauth-envelope.v1",
        "binding": "b" * 64,
        "wrappedKey": "wrapped",
        "nonce": "nonce",
        "ciphertext": "ciphertext",
    }

    repo.put(
        user_id="user-1",
        provider="google-gmail-readonly",
        record=envelope,
    )

    item = table.items[
        ("USER#user-1", "CONNECTION#google-gmail-readonly")
    ]
    assert item == {
        "PK": "USER#user-1",
        "SK": "CONNECTION#google-gmail-readonly",
        "envelope": envelope,
    }
    assert "ttl" not in item
    serialized = json.dumps(item)
    assert "access_token" not in serialized
    assert repo.get(
        user_id="user-1", provider="google-gmail-readonly"
    ) == envelope

    with pytest.raises(repository_module.RepositoryRecordError):
        repo.put(
            user_id="user-1",
            provider="google-gmail-readonly",
            record={**envelope, "access_token": "plaintext-secret"},
        )


def opportunity_record():
    return {
        "id": "opp_12345678",
        "userId": "user-1",
        "source": {
            "sourceId": "gmail:t1:m1",
            "threadId": "t1",
            "deepLink": "https://mail.google.com/mail/u/0/#inbox/t1",
            "correspondent": "person@example.net",
            "subject": "Follow up",
            "excerpt": "Bounded derived excerpt",
        },
        "waitingSince": "2026-07-10T12:00:00+00:00",
        "title": "Reply",
        "reason": "A person is waiting",
        "confidence": 0.9,
    }


def fence_item(*, generation=1, status="CONNECTED"):
    return {
        "PK": "USER#user-1",
        "SK": "GMAIL#CONNECTION_FENCE",
        "recordType": "GMAIL_CONNECTION_FENCE",
        "userId": "user-1",
        "generation": generation,
        "status": status,
        "updatedAt": int(NOW.timestamp()),
    }


def test_derived_opportunities_and_drafts_have_exact_fourteen_day_ttl():
    repo, table = repository()
    repo.replace_opportunities(
        user_id="user-1",
        records=[opportunity_record()],
        expires_at=DERIVED_TTL,
    )
    draft = models.DraftRevision.create(
        action_id="action_12345678",
        revision=2,
        to="person@example.net",
        subject="Following up",
        body="Hello again",
    )
    repo.save_draft(user_id="user-1", draft=draft, expires_at=DERIVED_TTL)

    opportunities = table.items[("USER#user-1", "GMAIL#OPPORTUNITIES")]
    assert opportunities["ttl"] == DERIVED_TTL
    assert opportunities["opportunities"][0]["confidence"] == Decimal("0.9")
    assert "raw" not in repr(opportunities).casefold()
    draft_item = table.items[
        ("USER#user-1", "GMAIL#DRAFT#action_12345678#0000000002")
    ]
    assert draft_item["ttl"] == DERIVED_TTL
    assert draft_item["draft"] == {
        "actionId": "action_12345678",
        "revision": 2,
        "to": "person@example.net",
        "subject": "Following up",
        "body": "Hello again",
        "payloadHash": draft.payload_hash,
    }
    draft_put = [
        call for name, call in table.calls
        if name == "put_item" and "#DRAFT#" in call["Item"]["SK"]
    ][0]
    assert draft_put["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )


def test_latest_draft_strongly_reads_the_highest_live_exact_revision():
    class QueryTable(FakeTable):
        def query(self, **kwargs):
            self.calls.append(("query", kwargs))
            values = kwargs["ExpressionAttributeValues"]
            items = sorted(
                (
                    dict(item)
                    for (pk, sk), item in self.items.items()
                    if pk == values[":pk"] and sk.startswith(values[":sk"])
                ),
                key=lambda item: item["SK"],
                reverse=kwargs["ScanIndexForward"] is False,
            )
            return {"Items": items[: kwargs["Limit"]]}

    table = QueryTable()
    repo, _ = repository(table)
    first = models.DraftRevision.create(
        action_id="action_12345678",
        revision=1,
        to="person@example.net",
        subject="First",
        body="First body",
    )
    latest = models.DraftRevision.create(
        action_id=first.action_id,
        revision=2,
        to=first.to,
        subject="Edited",
        body="Edited body",
    )
    repo.save_draft(user_id="user-1", draft=first, expires_at=DERIVED_TTL)
    repo.save_draft(user_id="user-1", draft=latest, expires_at=DERIVED_TTL)

    assert repo.latest_draft(
        user_id="user-1",
        action_id=first.action_id,
    ) == latest
    query = [call for name, call in table.calls if name == "query"][-1]
    assert query == {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": "USER#user-1",
            ":sk": "GMAIL#DRAFT#action_12345678#",
        },
        "ConsistentRead": True,
        "ScanIndexForward": False,
        "Limit": 1,
    }


def _action_record(*, state="PREPARED", revision=1):
    return {
        "PK": "USER#user-1",
        "SK": "ACTION#action_12345678",
        "actionId": "action_12345678",
        "userId": "user-1",
        "state": state,
        "revision": revision,
        "draftRevision": 1,
        "ttl": int((NOW + timedelta(days=14)).timestamp()),
    }


def _edited_draft(*, revision=2):
    return models.DraftRevision.create(
        action_id="action_12345678",
        revision=revision,
        to="person@example.net",
        subject=f"Edited {revision}",
        body=f"Edited body {revision}",
    )


def test_atomic_edit_stales_prepared_action_and_persists_revision_together():
    repo, table = repository()
    action = _action_record()
    table.items[(action["PK"], action["SK"])] = action
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = {
        "PK": "USER#user-1",
        "SK": "GMAIL#CONNECTION_FENCE",
        "recordType": "GMAIL_CONNECTION_FENCE",
        "userId": "user-1",
        "generation": 6,
        "status": "CONNECTED",
        "updatedAt": int(NOW.timestamp()),
    }
    draft = _edited_draft()

    outcome = repo.save_superseding_draft(
        user_id="user-1",
        action_id=draft.action_id,
        draft=draft,
        expected_draft_revision=1,
        current_draft_revision=2,
        expires_at=DERIVED_TTL,
        expected_generation=6,
    )

    assert outcome == {
        "draftPersisted": True,
        "actionId": draft.action_id,
        "userId": "user-1",
        "draftRevision": 2,
        "payloadHash": draft.payload_hash,
    }
    stored_action = table.items[(action["PK"], action["SK"])]
    assert stored_action["state"] == "STALE"
    assert stored_action["staleDraftRevision"] == 1
    assert stored_action["supersededByDraftRevision"] == 2
    stored_draft = table.items[
        ("USER#user-1", "GMAIL#DRAFT#action_12345678#0000000002")
    ]
    assert stored_draft["draft"]["payloadHash"] == draft.payload_hash
    assert stored_draft["connectionGeneration"] == 6


@pytest.mark.parametrize("state", ["PREPARED", "APPROVAL_PENDING", "STALE"])
def test_atomic_edit_emits_no_unused_dynamodb_expression_values(state):
    repo, table = repository()
    action = _action_record(state=state, revision=2)
    expected_revision = 1
    current_revision = 2
    if state == "STALE":
        action.update(
            {
                "staleReason": "newer-draft-revision",
                "staleDraftRevision": 1,
                "supersededByDraftRevision": 2,
            }
        )
        expected_revision = 2
        current_revision = 3
    table.items[(action["PK"], action["SK"])] = action

    repo.save_superseding_draft(
        user_id="user-1",
        action_id="action_12345678",
        draft=_edited_draft(revision=current_revision),
        expected_draft_revision=expected_revision,
        current_draft_revision=current_revision,
        expires_at=DERIVED_TTL,
    )

    transaction = next(
        call for name, call in table.calls if name == "transact_write_items"
    )
    for operation in transaction["TransactItems"]:
        subject = next(iter(operation.values()))
        supplied = set(subject.get("ExpressionAttributeValues", {}))
        expressions = " ".join(
            str(subject.get(field, ""))
            for field in ("ConditionExpression", "UpdateExpression")
        )
        used = set(re.findall(r":[A-Za-z][A-Za-z0-9]*", expressions))
        assert supplied == used


def test_atomic_edit_writes_nothing_when_action_appears_after_absent_read():
    class ApprovalWinsAbsentGap(FakeTable):
        def _before_transaction(self, _operations):
            action = _action_record(state="APPROVAL_PENDING", revision=2)
            self.items[(action["PK"], action["SK"])] = action

    repo, table = repository(ApprovalWinsAbsentGap())
    draft = _edited_draft()

    with pytest.raises(
        repository_module.DraftRevisionConflictError,
        match="action authority fence",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id=draft.action_id,
            draft=draft,
            expected_draft_revision=1,
            current_draft_revision=2,
            expires_at=DERIVED_TTL,
        )

    assert table.items[
        ("USER#user-1", "ACTION#action_12345678")
    ]["state"] == "APPROVAL_PENDING"
    assert (
        "USER#user-1",
        "GMAIL#DRAFT#action_12345678#0000000002",
    ) not in table.items


def test_atomic_edit_writes_nothing_when_approval_transition_wins():
    class ApprovalWinsPreparedRace(FakeTable):
        def _before_transaction(self, _operations):
            action = self.items[
                ("USER#user-1", "ACTION#action_12345678")
            ]
            action["state"] = "APPROVAL_PENDING"
            action["revision"] = 2

    table = ApprovalWinsPreparedRace()
    action = _action_record()
    table.items[(action["PK"], action["SK"])] = action
    repo, _ = repository(table)
    draft = _edited_draft()

    with pytest.raises(
        repository_module.DraftRevisionConflictError,
        match="action authority fence",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id=draft.action_id,
            draft=draft,
            expected_draft_revision=1,
            current_draft_revision=2,
            expires_at=DERIVED_TTL,
        )

    assert table.items[(action["PK"], action["SK"])]["state"] == (
        "APPROVAL_PENDING"
    )
    assert (
        "USER#user-1",
        "GMAIL#DRAFT#action_12345678#0000000002",
    ) not in table.items


def test_atomic_edit_advances_an_exact_stale_draft_chain_without_reopening():
    repo, table = repository()
    action = {
        **_action_record(state="STALE", revision=3),
        "staleReason": "newer-draft-revision",
        "staleDraftRevision": 1,
        "supersededByDraftRevision": 2,
    }
    table.items[(action["PK"], action["SK"])] = action
    draft = _edited_draft(revision=3)

    repo.save_superseding_draft(
        user_id="user-1",
        action_id=draft.action_id,
        draft=draft,
        expected_draft_revision=2,
        current_draft_revision=3,
        expires_at=DERIVED_TTL,
    )

    stored = table.items[(action["PK"], action["SK"])]
    assert stored["state"] == "STALE"
    assert stored["revision"] == 4
    assert stored["staleDraftRevision"] == 1
    assert stored["supersededByDraftRevision"] == 3


def test_atomic_stale_chain_edit_loses_cleanly_when_reprepare_wins():
    class ReprepareWinsStaleRace(FakeTable):
        def _before_transaction(self, _operations):
            action = self.items[
                ("USER#user-1", "ACTION#action_12345678")
            ]
            action["state"] = "PREPARED"
            action["revision"] = 4
            action["draftRevision"] = 2
            for field in {
                "staleReason",
                "staleDraftRevision",
                "supersededByDraftRevision",
            }:
                action.pop(field, None)

    table = ReprepareWinsStaleRace()
    action = {
        **_action_record(state="STALE", revision=3),
        "staleReason": "newer-draft-revision",
        "staleDraftRevision": 1,
        "supersededByDraftRevision": 2,
    }
    table.items[(action["PK"], action["SK"])] = action
    repo, _ = repository(table)

    with pytest.raises(
        repository_module.DraftRevisionConflictError,
        match="action authority fence",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id="action_12345678",
            draft=_edited_draft(revision=3),
            expected_draft_revision=2,
            current_draft_revision=3,
            expires_at=DERIVED_TTL,
        )

    assert table.items[
        ("USER#user-1", "ACTION#action_12345678")
    ]["state"] == "PREPARED"
    assert (
        "USER#user-1",
        "GMAIL#DRAFT#action_12345678#0000000003",
    ) not in table.items


@pytest.mark.parametrize("state", ["APPROVED", "DISPATCHING", "UNCERTAIN"])
def test_atomic_edit_refuses_advanced_or_uncertain_authority_without_a_put(
    state,
):
    repo, table = repository()
    action = _action_record(state=state, revision=3)
    table.items[(action["PK"], action["SK"])] = action

    with pytest.raises(
        repository_module.DraftRevisionConflictError,
        match="authority advanced",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id="action_12345678",
            draft=_edited_draft(),
            expected_draft_revision=1,
            current_draft_revision=2,
            expires_at=DERIVED_TTL,
        )

    assert not any(name == "transact_write_items" for name, _ in table.calls)
    assert (
        "USER#user-1",
        "GMAIL#DRAFT#action_12345678#0000000002",
    ) not in table.items


def test_atomic_edit_never_reconciles_from_the_draft_record_alone():
    class DraftWithoutProvenActionOutcome(FakeTable):
        def _before_transaction(self, operations):
            put = next(operation["Put"] for operation in operations if "Put" in operation)
            draft_item = self._decode_item(put["Item"])
            self.items[self._key(draft_item)] = draft_item
            action = _action_record(state="APPROVAL_PENDING", revision=2)
            self.items[(action["PK"], action["SK"])] = action
            raise AmbiguousWrite("transaction response lost")

    repo, table = repository(DraftWithoutProvenActionOutcome())

    with pytest.raises(
        repository_module.RepositoryRecordError,
        match="outcome is unproven",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id="action_12345678",
            draft=_edited_draft(),
            expected_draft_revision=1,
            current_draft_revision=2,
            expires_at=DERIVED_TTL,
        )

    assert table.items[
        ("USER#user-1", "ACTION#action_12345678")
    ]["state"] == "APPROVAL_PENDING"


@pytest.mark.parametrize(
    ("action_id", "current_revision"),
    [("action_other_456", 2), ("action_12345678", 999)],
)
def test_atomic_edit_repository_rejects_declared_draft_binding_mismatch(
    action_id,
    current_revision,
):
    repo, table = repository()

    with pytest.raises(
        repository_module.RepositoryRecordError,
        match="exact draft binding",
    ):
        repo.save_superseding_draft(
            user_id="user-1",
            action_id=action_id,
            draft=_edited_draft(),
            expected_draft_revision=1,
            current_draft_revision=current_revision,
            expires_at=DERIVED_TTL,
        )

    assert not any(name == "transact_write_items" for name, _ in table.calls)


def test_generation_guard_atomically_rejects_a_write_that_loses_to_disconnect():
    class DisconnectDuringOpportunityWrite(FakeTable):
        def _before_transaction(self, operations):
            if any(
                "Put" in operation
                and self._decode_item(operation["Put"]["Item"])["SK"]
                == "GMAIL#OPPORTUNITIES"
                for operation in operations
            ):
                self.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
                    "generation"
                ] = 2
                self.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
                    "status"
                ] = "DISCONNECTING"

    table = DisconnectDuringOpportunityWrite()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item()
    repo, _ = repository(table)

    with pytest.raises(repository_module.ConnectionFenceError):
        repo.replace_opportunities(
            user_id="user-1",
            records=[opportunity_record()],
            expires_at=DERIVED_TTL,
            expected_generation=1,
        )

    assert ("USER#user-1", "GMAIL#OPPORTUNITIES") not in table.items


@pytest.mark.parametrize(
    "target_sk",
    ["CONNECTION#google-gmail-readonly", "GMAIL#OPPORTUNITIES"],
)
def test_stale_guarded_writer_cannot_clobber_the_new_generation(target_sk):
    class NewGenerationWins(FakeTable):
        def __init__(self):
            super().__init__()
            self.superseded = False

        def put_item(self, **kwargs):
            item = kwargs["Item"]
            if item["SK"] == target_sk and not self.superseded:
                self.superseded = True
                self.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = (
                    fence_item(generation=2, status="CONNECTED")
                )
                self.items[("USER#user-1", target_sk)] = {
                    "PK": "USER#user-1",
                    "SK": target_sk,
                    "connectionGeneration": 2,
                    "newGeneration": True,
                }
            return super().put_item(**kwargs)

        def _before_transaction(self, operations):
            target = next(
                (
                    self._decode_item(operation["Put"]["Item"])
                    for operation in operations
                    if "Put" in operation
                    and self._decode_item(operation["Put"]["Item"])["SK"]
                    == target_sk
                ),
                None,
            )
            if target is not None and not self.superseded:
                self.superseded = True
                self.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = (
                    fence_item(generation=2, status="CONNECTED")
                )
                self.items[("USER#user-1", target_sk)] = {
                    "PK": "USER#user-1",
                    "SK": target_sk,
                    "connectionGeneration": 2,
                    "newGeneration": True,
                }

    table = NewGenerationWins()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item()
    table.items[("USER#user-1", target_sk)] = {
        "PK": "USER#user-1",
        "SK": target_sk,
        "connectionGeneration": 1,
    }
    repo, _ = repository(table)

    with pytest.raises(repository_module.ConnectionFenceError):
        if target_sk.startswith("CONNECTION#"):
            repo.put(
                user_id="user-1",
                provider="google-gmail-readonly",
                record={
                    "format": "personal-operator.oauth-envelope.v1",
                    "binding": "b" * 64,
                    "wrappedKey": "stale",
                    "nonce": "nonce",
                    "ciphertext": "ciphertext",
                },
                expected_generation=1,
            )
        else:
            repo.replace_opportunities(
                user_id="user-1",
                records=[opportunity_record()],
                expires_at=DERIVED_TTL,
                expected_generation=1,
            )

    assert table.items[("USER#user-1", target_sk)] == {
        "PK": "USER#user-1",
        "SK": target_sk,
        "connectionGeneration": 2,
        "newGeneration": True,
    }
    assert table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
        "generation"
    ] == 2


def test_fenced_delete_removes_a_target_only_under_the_exact_disconnecting_fence():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=3, status="DISCONNECTING"
    )
    table.items[("USER#user-1", "CONNECTION#google-gmail-readonly")] = {
        "PK": "USER#user-1",
        "SK": "CONNECTION#google-gmail-readonly",
        "connectionGeneration": 3,
        "envelope": {"format": "personal-operator.oauth-envelope.v1"},
    }

    repo.delete_under_disconnecting_fence(
        "user-1",
        3,
        {"PK": "USER#user-1", "SK": "CONNECTION#google-gmail-readonly"},
    )

    assert (
        "USER#user-1",
        "CONNECTION#google-gmail-readonly",
    ) not in table.items


def test_fenced_delete_cannot_destroy_a_reconnect_after_a_stale_runner_resumes():
    # A slow same-generation runner captured generation 3 while DISCONNECTING.
    # A faster runner finished the disconnect; the user reconnected, advancing
    # the fence to a CONNECTED generation with a fresh envelope. The stale
    # runner must NOT be able to delete that new envelope.
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4, status="CONNECTED"
    )
    reconnected = {
        "PK": "USER#user-1",
        "SK": "CONNECTION#google-gmail-readonly",
        "connectionGeneration": 4,
        "envelope": {"format": "personal-operator.oauth-envelope.v1", "new": True},
    }
    table.items[("USER#user-1", "CONNECTION#google-gmail-readonly")] = dict(
        reconnected
    )

    with pytest.raises(repository_module.ConnectionFenceError):
        repo.delete_under_disconnecting_fence(
            "user-1",
            3,
            {"PK": "USER#user-1", "SK": "CONNECTION#google-gmail-readonly"},
        )

    assert (
        table.items[("USER#user-1", "CONNECTION#google-gmail-readonly")]
        == reconnected
    )
    assert repo.connection_status("user-1") == "CONNECTED"


def test_fresh_disconnect_advances_an_already_disconnected_generation():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4,
        status="DISCONNECTED",
    )

    assert repo.begin_disconnect("user-1") == 5
    assert table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
        "status"
    ] == "DISCONNECTING"
    with pytest.raises(repository_module.ConnectionFenceError):
        repo.oauth_generation("user-1")

    repo.finish_disconnect("user-1", 5)
    assert table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] == fence_item(
        generation=5,
        status="DISCONNECTED",
    )


def test_activation_cannot_revive_a_disconnecting_fence():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4,
        status="DISCONNECTING",
    )
    table.items[
        ("USER#user-1", "CONNECTION#google-gmail-readonly")
    ] = {
        "PK": "USER#user-1",
        "SK": "CONNECTION#google-gmail-readonly",
        "connectionGeneration": 4,
        "envelope": {
            "format": "personal-operator.oauth-envelope.v1",
            "binding": "b" * 64,
            "wrappedKey": "wrapped",
            "nonce": "nonce",
            "ciphertext": "ciphertext",
        },
    }

    with pytest.raises(repository_module.ConnectionFenceError):
        repo.activate_connection("user-1", 4)


def test_disconnect_completion_requires_the_disconnecting_status():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4,
        status="CONNECTED",
    )

    with pytest.raises(repository_module.ConnectionFenceError):
        repo.finish_disconnect("user-1", 4)

    assert table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
        "status"
    ] == "CONNECTED"


def test_activation_requires_the_exact_generation_connection_record():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4,
        status="CONNECTED",
    )

    with pytest.raises(repository_module.ConnectionFenceError):
        repo.activate_connection("user-1", 4)


def test_connection_envelope_activation_and_refresh_are_generation_bound():
    repo, table = repository()
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")] = fence_item(
        generation=4,
        status="DISCONNECTED",
    )
    envelope = {
        "format": "personal-operator.oauth-envelope.v1",
        "binding": "b" * 64,
        "wrappedKey": "wrapped",
        "nonce": "nonce",
        "ciphertext": "ciphertext",
    }

    repo.put(
        user_id="user-1",
        provider="google-gmail-readonly",
        record=envelope,
        expected_generation=4,
        allow_disconnected=True,
    )
    repo.activate_connection("user-1", 4)

    connection = table.items[
        ("USER#user-1", "CONNECTION#google-gmail-readonly")
    ]
    assert connection["connectionGeneration"] == 4
    assert repo.connected_generation("user-1") == 4
    assert table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
        "status"
    ] == "CONNECTED"

    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")]["generation"] = 5
    table.items[("USER#user-1", "GMAIL#CONNECTION_FENCE")][
        "status"
    ] = "DISCONNECTED"
    with pytest.raises(repository_module.ConnectionFenceError):
        repo.put(
            user_id="user-1",
            provider="google-gmail-readonly",
            record=envelope,
            expected_generation=4,
        )


def test_draft_revision_is_immutable_but_exact_replay_is_idempotent():
    repo, table = repository()
    original = models.DraftRevision.create(
        action_id="action_12345678",
        revision=1,
        to="person@example.net",
        subject="Following up",
        body="  Exact body\r\n",
    )
    repo.save_draft(user_id="user-1", draft=original, expires_at=DERIVED_TTL)

    repo.save_draft(user_id="user-1", draft=original, expires_at=DERIVED_TTL)

    conflicting = models.DraftRevision.create(
        action_id="action_12345678",
        revision=1,
        to="person@example.net",
        subject="Following up",
        body="Different body",
    )
    with pytest.raises(repository_module.DraftRevisionConflictError):
        repo.save_draft(
            user_id="user-1",
            draft=conflicting,
            expires_at=DERIVED_TTL,
        )
    stored = table.items[
        ("USER#user-1", "GMAIL#DRAFT#action_12345678#0000000001")
    ]
    assert stored["draft"]["body"] == "  Exact body\r\n"
    assert stored["draft"]["payloadHash"] == original.payload_hash
    reconcile_reads = [
        call for name, call in table.calls
        if name == "get_item" and "#DRAFT#" in call["Key"]["SK"]
    ]
    assert reconcile_reads
    assert all(call["ConsistentRead"] is True for call in reconcile_reads)


def test_ambiguous_draft_write_reconciles_only_the_exact_payload_hash():
    class AmbiguousTable(FakeTable):
        def __init__(self, *, commit):
            super().__init__()
            self.commit = commit
            self.attempted = False

        def put_item(self, **kwargs):
            if "#DRAFT#" in kwargs["Item"]["SK"] and not self.attempted:
                self.attempted = True
                self.calls.append(("put_item", kwargs))
                if self.commit:
                    self.items[self._key(kwargs["Item"])] = kwargs["Item"]
                raise AmbiguousWrite("socket timed out after the write")
            return super().put_item(**kwargs)

    draft = models.DraftRevision.create(
        action_id="action_12345678",
        revision=1,
        to="person@example.net",
        subject="Following up",
        body="Exact body",
    )
    committed, _ = repository(AmbiguousTable(commit=True))
    committed.save_draft(
        user_id="user-1", draft=draft, expires_at=DERIVED_TTL
    )

    uncommitted, _ = repository(AmbiguousTable(commit=False))
    with pytest.raises(AmbiguousWrite):
        uncommitted.save_draft(
            user_id="user-1", draft=draft, expires_at=DERIVED_TTL
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: {**record, "rawBody": "secret"},
        lambda record: {
            **record,
            "source": {**record["source"], "payload": {"body": "secret"}},
        },
        lambda record: {**record, "userId": "another-user"},
    ],
)
def test_repository_rejects_raw_or_cross_user_opportunity_records(mutation):
    repo, table = repository()

    with pytest.raises(repository_module.RepositoryRecordError):
        repo.replace_opportunities(
            user_id="user-1",
            records=[mutation(opportunity_record())],
            expires_at=DERIVED_TTL,
        )

    assert table.items == {}


@pytest.mark.parametrize("expires_at", [True, 0, -1, "123", DERIVED_TTL - 60])
def test_repository_rejects_invalid_ttl_values(expires_at):
    repo, _ = repository()
    with pytest.raises(repository_module.RepositoryRecordError):
        repo.replace_opportunities(
            user_id="user-1",
            records=[opportunity_record()],
            expires_at=expires_at,
        )
