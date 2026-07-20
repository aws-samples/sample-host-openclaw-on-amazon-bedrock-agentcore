"""Active router logs retain bounded metadata and never private content."""

from __future__ import annotations

import json
import io
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


def test_propagating_dependency_logger_is_metadata_only(caplog) -> None:
    canaries = (
        "dependency payload secret",
        "dependency provider error",
        "/mnt/workspace/dependency-private.txt",
    )
    dependency = logging.getLogger("botocore.personal_operator_probe")
    caplog.set_level(logging.WARNING)

    with index._metadata_only_dependency_logging():
        try:
            raise RuntimeError(canaries[1])
        except RuntimeError:
            dependency.warning(
                "request=%s path=%s",
                canaries[0],
                canaries[2],
                exc_info=True,
                stack_info=True,
                extra={"private_payload": canaries[0]},
            )

    assert all(canary not in caplog.text for canary in canaries)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert json.loads(record.getMessage()) == {
        "component": "router",
        "event": "dependency_event",
        "level": "WARNING",
        "schema": "personal-operator.log.v1",
    }
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert "private_payload" not in record.__dict__


def test_late_dependency_handler_cannot_format_private_extra() -> None:
    sentinel = "late dependency private payload"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    original_formatter = logging.Formatter(
        "%(message)s private=%(private_payload)s"
    )
    handler.setFormatter(original_formatter)
    dependency = logging.getLogger("botocore.personal_operator_late_handler")
    previous_propagate = dependency.propagate
    previous_level = dependency.level
    dependency.propagate = False
    dependency.setLevel(logging.WARNING)

    try:
        with index._metadata_only_dependency_logging():
            dependency.addHandler(handler)
            dependency.warning("payload=%s", sentinel, extra={"private_payload": sentinel})
            dependency.removeHandler(handler)
    finally:
        dependency.propagate = previous_propagate
        dependency.setLevel(previous_level)

    assert sentinel not in stream.getvalue()
    assert json.loads(stream.getvalue()) == {
        "component": "router",
        "event": "dependency_event",
        "level": "WARNING",
        "schema": "personal-operator.log.v1",
    }
    assert handler.formatter is original_formatter


def test_boundary_runs_before_existing_handler_filters() -> None:
    sentinel = "existing filter private payload"
    observed: list[object] = []

    class ObservingFilter(logging.Filter):
        def filter(self, record):
            observed.append(record.__dict__.get("private_payload"))
            return True

    observing_filter = ObservingFilter()
    handler = logging.StreamHandler(io.StringIO())
    handler.addFilter(observing_filter)
    dependency = logging.getLogger("botocore.personal_operator_filter_order")
    previous_propagate = dependency.propagate
    previous_level = dependency.level
    dependency.propagate = False
    dependency.setLevel(logging.WARNING)
    dependency.addHandler(handler)

    try:
        with index._metadata_only_dependency_logging():
            dependency.warning("payload=%s", sentinel, extra={"private_payload": sentinel})
    finally:
        dependency.removeHandler(handler)
        dependency.propagate = previous_propagate
        dependency.setLevel(previous_level)

    assert observed == [None]
    assert handler.filters == [observing_filter]


def test_nested_boundary_restores_a_late_handler_exactly() -> None:
    handler = logging.NullHandler()
    original_formatter = logging.Formatter("original %(message)s")
    handler.setFormatter(original_formatter)
    dependency = logging.getLogger("botocore.personal_operator_nested")

    with index._metadata_only_dependency_logging():
        with index._metadata_only_dependency_logging():
            dependency.addHandler(handler)
        assert handler.formatter is index._METADATA_ONLY_FORMATTER
        dependency.removeHandler(handler)

    assert handler.formatter is original_formatter
