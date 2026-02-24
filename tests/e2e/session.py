"""DynamoDB session management for E2E tests.

Provides helpers to reset user sessions (forcing cold start) and
fully reset user data for clean-slate testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from tests.e2e.config import E2EConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResetResult:
    """Result of a session or user reset operation."""

    user_id: str
    session_deleted: bool
    items_deleted: int
    error: str = ""


def _get_table(config: E2EConfig):
    """Get the DynamoDB identity table resource."""
    dynamodb = boto3.resource("dynamodb", region_name=config.region)
    return dynamodb.Table(config.identity_table)


def _lookup_user_id(config: E2EConfig) -> str:
    """Look up the internal user_id from the Telegram channel mapping.

    Queries CHANNEL#telegram:{tg_user_id}:PROFILE to find the mapped user.
    Returns empty string if not found.
    """
    table = _get_table(config)
    channel_key = f"telegram:{config.user_id}"
    pk = f"CHANNEL#{channel_key}"

    try:
        resp = table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        item = resp.get("Item")
        if item:
            return item.get("userId", "")
    except ClientError as e:
        logger.warning("DynamoDB lookup failed for %s: %s", pk, e)

    return ""


def reset_session(config: E2EConfig) -> ResetResult:
    """Delete the session item for the user, forcing a new AgentCore microVM.

    Looks up user_id from channel mapping, then deletes
    PK=USER#{user_id}, SK=SESSION.
    """
    user_id = _lookup_user_id(config)
    if not user_id:
        return ResetResult(user_id="", session_deleted=False, items_deleted=0,
                           error="User not found in identity table")

    table = _get_table(config)
    try:
        resp = table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": "SESSION"},
            ReturnValues="ALL_OLD",
        )
        had_item = bool(resp.get("Attributes"))
        return ResetResult(
            user_id=user_id,
            session_deleted=had_item,
            items_deleted=1 if had_item else 0,
        )
    except ClientError as e:
        return ResetResult(user_id=user_id, session_deleted=False, items_deleted=0,
                           error=str(e))


def reset_user(config: E2EConfig) -> ResetResult:
    """Delete all DynamoDB items for the user (profile, channels, session).

    Removes:
    - CHANNEL#telegram:{tg_user_id}:PROFILE (channel -> user mapping)
    - USER#{user_id}:PROFILE (user profile)
    - USER#{user_id}:SESSION (active session)
    - USER#{user_id}:CHANNEL#* (all channel back-references)
    """
    table = _get_table(config)
    channel_key = f"telegram:{config.user_id}"
    items_deleted = 0
    errors: list[str] = []

    # 1. Look up user_id from channel mapping
    user_id = _lookup_user_id(config)

    # 2. Delete channel -> user mapping
    try:
        table.delete_item(Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"})
        items_deleted += 1
    except ClientError as e:
        errors.append(f"channel mapping: {e}")

    if not user_id:
        error_msg = "; ".join(errors) if errors else "User not found"
        return ResetResult(user_id="", session_deleted=False,
                           items_deleted=items_deleted, error=error_msg)

    user_pk = f"USER#{user_id}"

    # 3. Query all items under USER#{user_id} and delete them
    queried_items: list[dict] = []
    try:
        resp = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": user_pk},
        )
        queried_items = resp.get("Items", [])
        for item in queried_items:
            table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            items_deleted += 1
    except ClientError as e:
        errors.append(f"user items: {e}")

    session_deleted = any(item.get("SK") == "SESSION" for item in queried_items)

    return ResetResult(
        user_id=user_id,
        session_deleted=session_deleted,
        items_deleted=items_deleted,
        error="; ".join(errors) if errors else "",
    )
