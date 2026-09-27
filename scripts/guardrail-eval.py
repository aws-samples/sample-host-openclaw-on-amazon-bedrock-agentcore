#!/usr/bin/env python3
"""Evaluate prompts against a deployed Bedrock Guardrail version (OPERATOR TOOL).

Sends each fixture prompt to the bedrock-runtime ``ApplyGuardrail`` API with
``source=INPUT`` (the same side the proxy's ``guardContent`` tagging is assessed
on), prints one row per prompt -- id, expected verdict, actual verdict and which
policy fired -- and exits non-zero when any actual verdict differs from the
expected one. Use it to measure a guardrail change (e.g. PROMPT_ATTACK strength)
before and after deploying, or to check a new scheduled-task prompt before it
reaches production.

No model is invoked; ApplyGuardrail is billed per text unit like any guardrail
evaluation. Prompt text and PII matches are never printed.

Fixture format (see ``tests/fixtures/guardrail_prompts.json``)::

    {"prompts": [
        {"id": "brief", "expected": "allow", "schedule_name": "Morning brief",
         "text": "You are my briefer..."},
        {"id": "card", "expected": "block", "text": "My card is 4111 1111 1111 1111"}
    ]}

``expected`` is ``allow`` or ``block``. An entry with ``schedule_name`` is also
sent a second time as ``<id>+cron`` with the ``[Scheduled task: <name>] ``
prefix that ``lambda/cron/index.py`` adds, so you can see whether the prefix
itself changes the verdict. Anonymized-only results count as ``allow``.

Exit codes: 0 all matched, 1 at least one mismatch, 2 usage/fixture/API error.

Example
-------
    python3 scripts/guardrail-eval.py --guardrail-id <guardrail-id> --version <N> \\
        --fixtures tests/fixtures/guardrail_prompts.json --region us-west-2

IAM: the caller needs ``bedrock:ApplyGuardrail`` on the guardrail.
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

VERDICTS = ("allow", "block")


def cron_message(schedule_name, message):
    """Mirror of the cron Lambda's message framing (lambda/cron/index.py)."""
    return f"[Scheduled task: {schedule_name}] {message}"


def load_cases(path):
    """Read the fixture file and expand ``schedule_name`` entries into a +cron variant."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list) or not prompts:
        raise ValueError("fixture file must be an object with a non-empty 'prompts' list")

    cases, seen = [], set()
    for i, entry in enumerate(prompts):
        pid, expected, text = entry.get("id"), entry.get("expected"), entry.get("text")
        if not pid or not isinstance(text, str) or not text:
            raise ValueError(f"prompt #{i}: 'id' and non-empty 'text' are required")
        if expected not in VERDICTS:
            raise ValueError(f"prompt {pid!r}: 'expected' must be one of {VERDICTS}, got {expected!r}")
        variants = [(pid, text)]
        if entry.get("schedule_name"):
            variants.append((f"{pid}+cron", cron_message(entry["schedule_name"], text)))
        for vid, vtext in variants:
            if vid in seen:
                raise ValueError(f"duplicate prompt id {vid!r}")
            seen.add(vid)
            cases.append({"id": vid, "expected": expected, "text": vtext})
    return cases


def _hit(label, action, detected=None):
    """Format one policy finding; ``None`` when the entry is neither actioned nor detected."""
    action = action or "NONE"
    if action == "NONE":
        return (f"{label} NONE (detected)", False) if detected else None
    return f"{label} {action}", action == "BLOCKED"


def classify(response):
    """Return (verdict, [fired policy descriptions]) for one ApplyGuardrail response.

    Only type/name/confidence/strength/action are reported -- never ``match``,
    which holds the matched user text (e.g. a card number).
    """
    hits = []
    for a in response.get("assessments") or []:
        for t in (a.get("topicPolicy") or {}).get("topics") or []:
            hits.append(_hit(f"topicPolicy {t.get('name')}", t.get("action"), t.get("detected")))
        for f in (a.get("contentPolicy") or {}).get("filters") or []:
            label = (
                f"contentPolicy {f.get('type')} "
                f"(confidence {f.get('confidence', '?')}, strength {f.get('filterStrength', '?')})"
            )
            hits.append(_hit(label, f.get("action"), f.get("detected")))
        sens = a.get("sensitiveInformationPolicy") or {}
        for p in sens.get("piiEntities") or []:
            hits.append(_hit(f"sensitiveInformation {p.get('type')}", p.get("action"), p.get("detected")))
        for r in sens.get("regexes") or []:
            hits.append(_hit(f"sensitiveInformation regex {r.get('name')}", r.get("action"), r.get("detected")))
        words = a.get("wordPolicy") or {}
        for w in words.get("customWords") or []:
            hits.append(_hit("wordPolicy custom word", w.get("action"), w.get("detected")))
        for w in words.get("managedWordLists") or []:
            hits.append(_hit(f"wordPolicy {w.get('type')}", w.get("action"), w.get("detected")))

    hits = [h for h in hits if h]
    fired = [desc for desc, _ in hits]
    blocked = any(is_block for _, is_block in hits)
    if response.get("action") == "GUARDRAIL_INTERVENED" and not hits:
        return "block", ["(intervened, no policy detail)"]
    return ("block" if blocked else "allow"), fired


def _error_text(exc):
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'ClientError')}: {err.get('Message', '')}".strip()
    return f"{type(exc).__name__}: {exc}"


def _print_table(rows):
    headers = ("id", "expected", "actual", "result", "policy fired")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers[:4])]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths), "-" * len(headers[4])))
    for r in rows:
        print(fmt.format(*r))


def parse_args(argv):
    p = argparse.ArgumentParser(description="Run fixture prompts through Bedrock ApplyGuardrail (INPUT).")
    p.add_argument("--guardrail-id", required=True, help="Guardrail id (OpenClawGuardrails output GuardrailId)")
    p.add_argument("--version", required=True, help="Guardrail version number, or DRAFT")
    p.add_argument("--fixtures", required=True, help="Path to the prompts JSON file")
    p.add_argument("--region", help="AWS region (default: the SDK's configured region)")
    return p.parse_args(argv)


def main(argv=None, client=None):
    args = parse_args(argv)
    try:
        cases = load_cases(args.fixtures)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load fixtures {args.fixtures}: {exc}", file=sys.stderr)
        return 2

    if client is None:
        client = boto3.client("bedrock-runtime", region_name=args.region)

    rows, mismatches, errors = [], 0, 0
    for case in cases:
        try:
            resp = client.apply_guardrail(
                guardrailIdentifier=args.guardrail_id,
                guardrailVersion=str(args.version),
                source="INPUT",
                content=[{"text": {"text": case["text"]}}],
            )
        except (ClientError, BotoCoreError) as exc:
            errors += 1
            msg = _error_text(exc)
            print(f"error: {case['id']}: {msg}", file=sys.stderr)
            rows.append((case["id"], case["expected"], "-", "ERROR", msg))
            continue
        actual, fired = classify(resp)
        ok = actual == case["expected"]
        mismatches += 0 if ok else 1
        rows.append((case["id"], case["expected"], actual, "ok" if ok else "MISMATCH", "; ".join(fired) or "-"))

    _print_table(rows)
    matched = len(cases) - mismatches - errors
    print(f"\n{matched}/{len(cases)} matched, {mismatches} mismatch(es), {errors} error(s)")
    if errors:
        return 2
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
