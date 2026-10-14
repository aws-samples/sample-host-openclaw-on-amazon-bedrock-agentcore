#!/usr/bin/env python3
"""Validate the operator's per-user model allowlist (`user_selectable_models`).

The cdk.json context key maps a short alias that users type in `/model <alias>`
to a Bedrock model or inference profile ID. An empty or missing map disables
per-user model switching. CDK synth and the deployment wrapper both call
`parse_user_selectable_models` so a malformed map fails before anything is
deployed. This module has no third-party dependencies, so the SDK deployment
environment can run it without CDK installed.

CLI: `python scripts/model_selection.py <cdk.json>` prints the allowlisted model
IDs as a comma-separated list (empty when the feature is disabled).
"""

import json
import re
import sys

CONTEXT_KEY = "user_selectable_models"
MAX_MODELS = 10
# "default" is reserved: `/model default` clears the user's override.
RESERVED_ALIASES = frozenset({"default"})
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Bedrock foundation-model IDs and system inference-profile IDs, e.g.
# "global.anthropic.claude-sonnet-5-5" or "anthropic.claude-3-haiku-20240307-v1:0".
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}(?::[0-9]{1,3})?$")


def parse_user_selectable_models(value):
    """Return a validated {alias: model_id} dict; raise ValueError when invalid.

    None, "" and {} all mean "feature disabled" and return {}. A JSON string is
    accepted so the value can also be passed with `cdk synth -c key='{...}'`.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{CONTEXT_KEY} must be a JSON object of alias to model ID") from error
    if not isinstance(value, dict):
        raise ValueError(f"{CONTEXT_KEY} must be an object mapping alias to model ID")
    if len(value) > MAX_MODELS:
        raise ValueError(f"{CONTEXT_KEY} allows at most {MAX_MODELS} models")
    models = {}
    for alias, model_id in value.items():
        if not isinstance(alias, str) or not ALIAS_PATTERN.match(alias):
            raise ValueError(
                f"{CONTEXT_KEY} alias {alias!r} must be 1-32 lowercase letters, digits, '-' or '_'")
        if alias in RESERVED_ALIASES:
            raise ValueError(f"{CONTEXT_KEY} alias {alias!r} is reserved")
        if not isinstance(model_id, str) or not MODEL_ID_PATTERN.match(model_id):
            raise ValueError(f"{CONTEXT_KEY}[{alias!r}] must be a Bedrock model or inference profile ID")
        models[alias] = model_id
    return models


def allowlisted_model_ids(models):
    """Distinct model IDs in alias order, as the runtime environment value."""
    return ",".join(dict.fromkeys(models.values()))


def main(argv):
    if len(argv) != 2:
        print("usage: model_selection.py <cdk.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        context = json.load(handle).get("context", {})
    try:
        models = parse_user_selectable_models(context.get(CONTEXT_KEY))
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(allowlisted_model_ids(models))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
