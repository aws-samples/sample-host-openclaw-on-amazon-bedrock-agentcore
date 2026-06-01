"""Custom-resource bootstrap hooks for post-deploy initialization."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

secretsmanager = boto3.client("secretsmanager")
dynamodb = boto3.client("dynamodb")


def _set_telegram_webhook(*, token: str, api_url: str, webhook_secret: str) -> None:
    query = urllib.parse.urlencode(
        {
            "url": f"{api_url}webhook/telegram",
            "secret_token": webhook_secret,
        }
    )
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook?{query}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise ValueError(
            f"Telegram setWebhook failed with HTTP {err.code}: {body}"
        ) from err
    except urllib.error.URLError as err:
        raise ValueError(f"Telegram setWebhook failed: {err.reason}") from err

    if not payload.get("ok"):
        raise ValueError(
            "Telegram setWebhook returned ok=false: "
            f"{json.dumps(payload, separators=(',', ':'))}"
        )


def _allowlist_telegram_admin(*, identity_table_name: str, admin_user_id: str) -> None:
    if not admin_user_id.isdigit():
        raise ValueError(
            "TelegramAdminUserId must contain digits only when provided."
        )

    channel_key = f"telegram:{admin_user_id}"
    dynamodb.put_item(
        TableName=identity_table_name,
        Item={
            "PK": {"S": f"ALLOW#{channel_key}"},
            "SK": {"S": "ALLOW"},
            "channelKey": {"S": channel_key},
            "addedAt": {"S": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
    )


def on_event(event, _context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]

    physical_id = props.get("PhysicalResourceId") or "telegram-bootstrap"

    if request_type == "Delete":
        return {"PhysicalResourceId": physical_id}

    telegram_bot_token = (props.get("TelegramBotToken") or "").strip()
    telegram_secret_id = props["TelegramTokenSecretId"]
    webhook_secret_id = props["WebhookSecretId"]
    identity_table_name = props["IdentityTableName"]
    telegram_admin_user_id = (props.get("TelegramAdminUserId") or "").strip()
    api_url = props["ApiUrl"]

    updated_secret = False
    configured_webhook = False
    allowlisted_admin = False

    if telegram_bot_token:
        secretsmanager.update_secret(
            SecretId=telegram_secret_id,
            SecretString=telegram_bot_token,
        )
        updated_secret = True

        webhook_secret = secretsmanager.get_secret_value(
            SecretId=webhook_secret_id
        )["SecretString"]
        _set_telegram_webhook(
            token=telegram_bot_token,
            api_url=api_url,
            webhook_secret=webhook_secret,
        )
        configured_webhook = True

    if telegram_admin_user_id:
        _allowlist_telegram_admin(
            identity_table_name=identity_table_name,
            admin_user_id=telegram_admin_user_id,
        )
        allowlisted_admin = True

    LOGGER.info(
        "Bootstrap complete: secret_updated=%s webhook_configured=%s admin_allowlisted=%s",
        updated_secret,
        configured_webhook,
        allowlisted_admin,
    )

    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "TelegramSecretUpdated": str(updated_secret).lower(),
            "TelegramWebhookConfigured": str(configured_webhook).lower(),
            "TelegramAdminAllowlisted": str(allowlisted_admin).lower(),
        },
    }
