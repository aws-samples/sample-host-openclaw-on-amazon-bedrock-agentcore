"""Active router logs retain bounded metadata and never private content."""

from __future__ import annotations

import json
import logging
import os


os.environ.setdefault("IDENTITY_TABLE_NAME", "personal-operator-test")
os.environ.setdefault(
    "USER_FILES_BUCKET", "openclaw-user-files-123456789012-eu-west-1"
)
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import index


def test_router_logger_drops_payload_identifiers_provider_errors_and_stacks(
    caplog,
) -> None:
    canaries = (
        "user_private_01",
        "telegram:998877",
        "session_private_01",
        "workspace body secret",
        "provider token secret",
        "/mnt/workspace/private.txt",
        "provider endpoint exploded",
    )
    caplog.set_level(logging.INFO)

    index.logger.info(
        "Telegram: user=%s actor=%s session=%s response=%s token=%s path=%s",
        *canaries[:6],
    )
    try:
        raise RuntimeError(canaries[6])
    except RuntimeError:
        index.logger.error("provider failed: %s", canaries[6], exc_info=True)

    rendered = caplog.text
    assert all(canary not in rendered for canary in canaries)
    payloads = [json.loads(record.getMessage()) for record in caplog.records]
    assert payloads == [
        {
            "component": "router",
            "event": "runtime_event",
            "level": "INFO",
            "schema": "personal-operator.log.v1",
        },
        {
            "component": "router",
            "event": "runtime_event",
            "level": "ERROR",
            "schema": "personal-operator.log.v1",
        },
    ]
    assert all(record.exc_info is None for record in caplog.records)
