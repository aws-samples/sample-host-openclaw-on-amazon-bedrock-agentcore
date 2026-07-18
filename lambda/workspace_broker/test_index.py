from datetime import datetime, timezone

import pytest

from router.workspace_capability import WorkspaceCapabilitySigner
from workspace_broker.index import (
    WorkspaceBrokerAuthorizationError,
    WorkspaceCredentialBroker,
)


ACCOUNT = "123456789012"
USER = "user_A"
OTHER_USER = "user_B"
SESSION = "ses_123456789012345678901234567890"
OTHER_SESSION = "ses_abcdefghijklmnopqrstuvwxyz123456"
AUDIENCE = "personal-operator-workspace-credential-broker"
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
    "12345678-1234-1234-1234-123456789abc:1"
)
RUNTIME_QUALIFIER = "release_" + "a" * 40
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/openclaw-workspace-session-role-eu-west-1"
CMK_ARN = f"arn:aws:kms:eu-west-1:{ACCOUNT}:key/test-key"
SECRET = b"k" * 64


class Table:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {"Item": dict(self.item)} if self.item is not None else {}


class Sts:
    def __init__(self, *, expiration=1_800):
        self.calls = []
        self.expiration = expiration

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.fromtimestamp(
                    self.expiration, tz=timezone.utc
                ),
            }
        }


def token(*, user=USER, session=SESSION, now=1_000):
    return WorkspaceCapabilitySigner(
        key_provider=lambda: SECRET,
        audience=AUDIENCE,
        clock=lambda: now,
        ttl_seconds=900,
    ).mint(user_id=user, session_id=session)


_DEFAULT_ITEM = object()


def broker(*, item=_DEFAULT_ITEM, now=1_001, expiration=1_800):
    table = Table(
        {
            "userId": USER,
            "sessionId": SESSION,
            "state": "BUSY",
            "tombstonedAt": None,
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
        }
        if item is _DEFAULT_ITEM
        else item
    )
    sts = Sts(expiration=expiration)
    instance = WorkspaceCredentialBroker(
        runtime_state_table=table,
        sts_client=sts,
        key_provider=lambda: SECRET,
        audience=AUDIENCE,
        workspace_role_arn=ROLE_ARN,
        workspace_bucket="personal-operator-workspace",
        workspace_cmk_arn=CMK_ARN,
        runtime_arn=RUNTIME_ARN,
        runtime_qualifier=RUNTIME_QUALIFIER,
        clock=lambda: now,
    )
    return instance, table, sts


def test_broker_strongly_verifies_live_session_and_derives_exact_policy():
    instance, table, sts = broker()

    result = instance.issue({"capability": token()})

    assert table.calls == [{"Key": {"userId": USER}, "ConsistentRead": True}]
    assert len(sts.calls) == 1
    request = sts.calls[0]
    assert request["RoleArn"] == ROLE_ARN
    assert request["DurationSeconds"] == 900
    assert request["RoleSessionName"].startswith("workspace-")
    assert request["Policy"] == (
        '{"Statement":[{"Action":["s3:GetObject","s3:PutObject",'
        '"s3:DeleteObject"],"Effect":"Allow","Resource":'
        '"arn:aws:s3:::personal-operator-workspace/user_A/*"},'
        '{"Action":"s3:ListBucket","Condition":{"StringLike":'
        '{"s3:prefix":"user_A/*"}},"Effect":"Allow","Resource":'
        '"arn:aws:s3:::personal-operator-workspace"},{"Action":'
        '["kms:Encrypt","kms:Decrypt","kms:GenerateDataKey"],"Condition":'
        '{"StringEquals":{"kms:CallerAccount":"123456789012",'
        '"kms:ViaService":"s3.eu-west-1.amazonaws.com"}},"Effect":"Allow",'
        '"Resource":"arn:aws:kms:eu-west-1:123456789012:key/test-key"}],'
        '"Version":"2012-10-17"}'
    )
    assert result == {
        "Version": 1,
        "AccessKeyId": "ASIAEXAMPLE",
        "SecretAccessKey": "secret",
        "SessionToken": "token",
        "Expiration": result["Expiration"],
    }
    assert result["Expiration"].endswith("+00:00")


@pytest.mark.parametrize(
    "item",
    [
        None,
        {
            "userId": USER,
            "sessionId": OTHER_SESSION,
            "state": "BUSY",
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
        },
        {
            "userId": USER,
            "sessionId": SESSION,
            "state": "DELETING",
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
        },
        {
            "userId": USER,
            "sessionId": SESSION,
            "state": "IDLE",
            "tombstonedAt": 900,
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
        },
        {
            "userId": USER,
            "sessionId": SESSION,
            "state": "IDLE",
            "runtimeArn": RUNTIME_ARN.replace(":1", ":2"),
            "runtimeQualifier": RUNTIME_QUALIFIER,
        },
        {
            "userId": USER,
            "sessionId": SESSION,
            "state": "IDLE",
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": "OLD_RELEASE",
        },
    ],
)
def test_broker_rejects_missing_stale_deleting_or_tombstoned_session_before_sts(item):
    instance, _, sts = broker(item=item)

    with pytest.raises(WorkspaceBrokerAuthorizationError, match="session"):
        instance.issue({"capability": token()})

    assert sts.calls == []


def test_broker_rejects_token_tamper_and_caller_supplied_authority_before_sts():
    instance, _, sts = broker()
    original = token()
    tampered = original[:-1] + ("A" if original[-1] != "A" else "B")

    for event in [
        {"capability": tampered},
        {"capability": original, "namespace": OTHER_USER},
        {"capability": original, "roleArn": ROLE_ARN},
    ]:
        with pytest.raises(WorkspaceBrokerAuthorizationError):
            instance.issue(event)

    assert sts.calls == []


def test_broker_rejects_a_valid_other_user_capability_against_this_runtime_record():
    instance, table, sts = broker()

    with pytest.raises(WorkspaceBrokerAuthorizationError, match="session"):
        instance.issue(
            {
                "capability": token(
                    user=OTHER_USER,
                    session=OTHER_SESSION,
                )
            }
        )

    assert table.calls == [
        {"Key": {"userId": OTHER_USER}, "ConsistentRead": True}
    ]
    assert sts.calls == []


def test_broker_rejects_sts_credentials_beyond_the_fixed_max_lifetime():
    instance, _, sts = broker(now=1_001, expiration=1_932)

    with pytest.raises(RuntimeError, match="lifetime"):
        instance.issue({"capability": token()})

    assert len(sts.calls) == 1
