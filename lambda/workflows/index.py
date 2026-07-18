"""Trusted product workflow orchestration.

This module is intentionally independent of the OpenClaw runtime. Provider
credentials and raw Gmail responses stay inside injected trusted adapters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

if __package__:
    from .gmail.models import DraftRevision, Opportunity
else:  # direct file loading in isolated unit tests
    from gmail_models import DraftRevision, Opportunity


DERIVED_RECORD_TTL = timedelta(days=14)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _opportunity_record(item: Opportunity) -> dict[str, object]:
    source = item.source
    return {
        "id": item.id,
        "userId": item.user_id,
        "source": {
            "sourceId": source.source_id,
            "threadId": source.thread_id,
            "deepLink": source.deep_link,
            "correspondent": source.correspondent,
            "subject": source.subject,
            "excerpt": source.excerpt,
        },
        "waitingSince": item.waiting_since.isoformat(),
        "title": item.title,
        "reason": item.reason,
        "confidence": item.confidence,
    }


class GmailPilotWorkflow:
    """Read-only pilot scan and draft preparation; no effect capability."""

    def __init__(self, *, scanner, ranker, repository, now: Callable[[], datetime] | None = None):
        self._scanner = scanner
        self._ranker = ranker
        self._repository = repository
        self._now = now or _utc_now

    def _expires_at(self) -> int:
        return int(
            (self._now().astimezone(timezone.utc) + DERIVED_RECORD_TTL).timestamp()
        )

    def scan(
        self,
        *,
        user_id: str,
        expected_generation: int | None = None,
    ) -> list[Opportunity]:
        sources = self._scanner.scan()
        opportunities = self._ranker.rank(user_id=user_id, sources=sources)
        arguments = {
            "user_id": user_id,
            "records": [_opportunity_record(item) for item in opportunities],
            "expires_at": self._expires_at(),
        }
        if expected_generation is not None:
            arguments["expected_generation"] = expected_generation
        self._repository.replace_opportunities(
            **arguments,
        )
        return opportunities

    @staticmethod
    def render_card(item: Opportunity) -> dict[str, object]:
        if not isinstance(item, Opportunity):
            raise TypeError("card requires a validated Opportunity")
        return {
            "id": item.id,
            "title": item.title,
            "reason": item.reason,
            "waitingSince": item.waiting_since.isoformat(),
            "sourceUrl": item.source.deep_link,
            "buttons": [
                {"label": "Edit", "action": "edit", "opportunityId": item.id},
                {
                    "label": "Prepare",
                    "action": "prepare",
                    "opportunityId": item.id,
                },
                {"label": "Skip", "action": "skip", "opportunityId": item.id},
                {"label": "Why", "action": "why", "opportunityId": item.id},
            ],
        }

    def prepare_draft(
        self,
        *,
        user_id: str,
        action_id: str,
        revision: int,
        to: str,
        subject: str,
        body: str,
        expected_generation: int | None = None,
    ) -> DraftRevision:
        draft = DraftRevision.create(
            action_id=action_id,
            revision=revision,
            to=to,
            subject=subject,
            body=body,
        )
        arguments = {
            "user_id": user_id,
            "draft": draft,
            "expires_at": self._expires_at(),
        }
        if expected_generation is not None:
            arguments["expected_generation"] = expected_generation
        self._repository.save_draft(**arguments)
        return draft
