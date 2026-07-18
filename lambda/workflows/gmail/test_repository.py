from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

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
    def __init__(self):
        self.items = {}
        self.calls = []

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
        old = self.items.pop(self._key(kwargs["Key"]), None)
        return {"Attributes": old} if old is not None else {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": item} if item is not None else {}


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
