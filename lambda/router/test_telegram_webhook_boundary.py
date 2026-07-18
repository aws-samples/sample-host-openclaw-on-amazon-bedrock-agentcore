"""Hostile boundary tests for Telegram webhook parsing and secret failures."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("IDENTITY_TABLE_NAME", "personal-operator-test")
os.environ.setdefault(
    "USER_FILES_BUCKET", "openclaw-user-files-123456789012-eu-west-1"
)
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import index


SECRET = "telegram-webhook-secret"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


def _event(body: str, *, headers: object = HEADERS) -> dict[str, object]:
    return {
        "requestContext": {
            "http": {"method": "POST", "path": "/webhook/telegram"}
        },
        "headers": headers,
        "body": body,
    }


def _valid_body() -> str:
    return json.dumps(
        {
            "update_id": 123,
            "message": {
                "chat": {"id": 7},
                "from": {"id": 11},
                "text": "hello",
            },
        }
    )


@pytest.mark.parametrize(
    "body",
    [
        '{"update_id":123,"update_id":124,"message":{}}',
        '{"update_id":123,"message":{"chat":{"id":7,"id":8},'
        '"from":{"id":11},"text":"hello"}}',
    ],
)
def test_duplicate_object_keys_are_rejected_before_ingress_mutation(body):
    ingress = MagicMock()

    with (
        patch.object(index, "_get_webhook_secret", return_value=SECRET),
        patch.object(index, "_get_telegram_ingress", return_value=ingress),
    ):
        response = index.handler(_event(body), None)

    assert response == {"statusCode": 400, "body": "Invalid update"}
    ingress.handle.assert_not_called()


def test_missing_or_wrong_supplied_secret_stays_unauthorized_before_body_parse():
    ingress = MagicMock()
    duplicate_body = '{"update_id":123,"update_id":124}'

    with (
        patch.object(index, "_get_webhook_secret", return_value=SECRET) as provider,
        patch.object(index, "_get_telegram_ingress", return_value=ingress),
    ):
        missing = index.handler(_event(duplicate_body, headers={}), None)
        wrong = index.handler(
            _event(
                duplicate_body,
                headers={"x-telegram-bot-api-secret-token": "wrong"},
            ),
            None,
        )

    assert missing == {"statusCode": 401, "body": "Unauthorized"}
    assert wrong == {"statusCode": 401, "body": "Unauthorized"}
    # A missing supplied credential is rejected without depending on the store;
    # a supplied credential must be checked against the configured value.
    assert provider.call_count == 1
    ingress.handle.assert_not_called()


def test_secret_provider_outage_is_retryable_and_never_reaches_ingress(caplog):
    ingress = MagicMock()
    provider_error = RuntimeError("vault details and secret material")

    with (
        patch.object(index, "_get_webhook_secret", side_effect=provider_error),
        patch.object(index, "_get_telegram_ingress", return_value=ingress),
    ):
        response = index.handler(_event(_valid_body()), None)

    assert response == {"statusCode": 503, "body": "service unavailable"}
    assert "vault details" not in response["body"]
    assert "secret material" not in response["body"]
    assert "vault details" not in caplog.text
    assert "secret material" not in caplog.text
    ingress.handle.assert_not_called()


def test_secret_fetch_failure_is_typed_and_logs_no_identifier_or_provider_detail(caplog):
    index._token_cache.clear()
    error = RuntimeError("backend endpoint and credential material")
    secret_id = "sensitive/telegram/webhook/name"

    with patch.object(index.secrets_client, "get_secret_value", side_effect=error):
        with pytest.raises(index.SecretProviderUnavailable, match="secret provider unavailable"):
            index._get_secret(secret_id)

    assert secret_id not in caplog.text
    assert "backend endpoint" not in caplog.text
    assert "credential material" not in caplog.text


def test_valid_auth_still_checks_body_bound_before_json_parsing():
    ingress = MagicMock()
    oversized = "{" + ("x" * (128 * 1024))

    with (
        patch.object(index, "_get_webhook_secret", return_value=SECRET),
        patch.object(index, "_get_telegram_ingress", return_value=ingress),
        patch.object(index.json, "loads", wraps=json.loads) as loads,
    ):
        response = index.handler(_event(oversized), None)

    assert response == {"statusCode": 400, "body": "Invalid update"}
    loads.assert_not_called()
    ingress.handle.assert_not_called()
