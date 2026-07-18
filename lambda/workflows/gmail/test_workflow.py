import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
GMAIL_DIR = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models = _load("gmail_models", GMAIL_DIR / "models.py")
workflow_module = _load("gmail_workflow_index", ROOT / "index.py")
NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


def source(index=1):
    return models.SourceEvidence(
        source_id=f"gmail:t{index}:m{index}",
        thread_id=f"t{index}",
        deep_link=f"https://mail.google.com/mail/u/0/#inbox/t{index}",
        correspondent=f"person{index}@example.net",
        subject="Follow up",
        excerpt="A bounded derived excerpt",
        waiting_since=NOW - timedelta(days=8),
    )


class Scanner:
    def __init__(self, sources):
        self.sources = sources

    def scan(self):
        return list(self.sources)


class Ranker:
    def rank(self, *, user_id, sources):
        return [
            models.Opportunity(
                id="opp_12345678",
                user_id=user_id,
                source=sources[0],
                waiting_since=sources[0].waiting_since,
                title="Reply to this person",
                reason="They are waiting on a concrete answer",
                confidence=0.9,
            )
        ]


class Repository:
    def __init__(self):
        self.saved = []
        self.drafts = []

    def replace_opportunities(self, *, user_id, records, expires_at):
        self.saved.append((user_id, records, expires_at))

    def save_draft(self, *, user_id, draft, expires_at):
        self.drafts.append((user_id, draft, expires_at))


def test_scan_persists_only_derived_records_with_fourteen_day_ttl():
    repo = Repository()
    workflow = workflow_module.GmailPilotWorkflow(
        scanner=Scanner([source()]),
        ranker=Ranker(),
        repository=repo,
        now=lambda: NOW,
    )

    opportunities = workflow.scan(user_id="user-1")

    assert len(opportunities) == 1
    user_id, records, expires_at = repo.saved[0]
    assert user_id == "user-1"
    assert expires_at == int((NOW + timedelta(days=14)).timestamp())
    assert records == [
        {
            "id": "opp_12345678",
            "userId": "user-1",
            "source": {
                "sourceId": "gmail:t1:m1",
                "threadId": "t1",
                "deepLink": "https://mail.google.com/mail/u/0/#inbox/t1",
                "correspondent": "person1@example.net",
                "subject": "Follow up",
                "excerpt": "A bounded derived excerpt",
            },
            "waitingSince": "2026-07-10T12:00:00+00:00",
            "title": "Reply to this person",
            "reason": "They are waiting on a concrete answer",
            "confidence": 0.9,
        }
    ]
    assert "raw" not in repr(records).casefold()


def test_cards_have_edit_prepare_skip_why_and_never_send():
    workflow = workflow_module.GmailPilotWorkflow(
        scanner=Scanner([source()]),
        ranker=Ranker(),
        repository=Repository(),
        now=lambda: NOW,
    )
    opportunity = workflow.scan(user_id="user-1")[0]

    card = workflow.render_card(opportunity)

    assert [button["label"] for button in card["buttons"]] == [
        "Edit",
        "Prepare",
        "Skip",
        "Why",
    ]
    assert all(button["action"] != "send" for button in card["buttons"])
    assert card["sourceUrl"] == opportunity.source.deep_link


def test_prepared_draft_is_exactly_hashed_but_has_no_dispatch_transition():
    repo = Repository()
    workflow = workflow_module.GmailPilotWorkflow(
        scanner=Scanner([]),
        ranker=Ranker(),
        repository=repo,
        now=lambda: NOW,
    )

    draft = workflow.prepare_draft(
        user_id="user-1",
        action_id="action_12345678",
        revision=1,
        to="person@example.net",
        subject="Following up",
        body="Hello again",
    )

    assert draft.payload_hash == models.DraftRevision.compute_payload_hash(
        to="person@example.net", subject="Following up", body="Hello again"
    )
    assert repo.drafts[0][2] == int((NOW + timedelta(days=14)).timestamp())
    assert not hasattr(workflow, "send")
    assert not hasattr(workflow, "dispatch")
