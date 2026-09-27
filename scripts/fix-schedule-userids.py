#!/usr/bin/env python3
"""Patch EventBridge schedule payloads to use the current userId.

Fixes schedules created with stale userIds (before deterministic userId fix).
Safe to run multiple times — skips schedules already using the correct userId.

Usage:
    python3 scripts/fix-schedule-userids.py \\
        --namespace telegram_<telegram_id> --user-id user_<16 hex> \\
        [--stale-id user_<16 hex> ...] [--dry-run]

--namespace / --user-id may also be set via OPENCLAW_NAMESPACE /
OPENCLAW_USER_ID. The namespace itself is always treated as a stale userId.
"""
import json

from _ns_args import parse_args

ARGS = parse_args(__doc__.splitlines()[0], with_stale_ids=True)

import boto3  # noqa: E402

REGION = "ap-southeast-2"
SCHEDULE_GROUP = "openclaw-cron"
NAMESPACE_PREFIX = f"openclaw-{ARGS.namespace}-"
CURRENT_USER_ID = ARGS.user_id
STALE_IDS = set(ARGS.stale_id) | {ARGS.namespace}

DRY_RUN = ARGS.dry_run

scheduler = boto3.client("scheduler", region_name=REGION)

# List all schedules in the group
paginator = scheduler.get_paginator("list_schedules")
schedules = []
for page in paginator.paginate(GroupName=SCHEDULE_GROUP):
    for s in page["Schedules"]:
        if s["Name"].startswith(NAMESPACE_PREFIX):
            schedules.append(s["Name"])

print(f"Found {len(schedules)} schedules for {ARGS.namespace}")

patched = 0
skipped = 0
errors = 0

for name in sorted(schedules):
    try:
        sched = scheduler.get_schedule(Name=name, GroupName=SCHEDULE_GROUP)
    except Exception as e:
        print(f"  ERROR reading {name}: {e}")
        errors += 1
        continue

    target_input = sched["Target"].get("Input", "{}")
    try:
        payload = json.loads(target_input)
    except json.JSONDecodeError:
        print(f"  SKIP {name}: non-JSON payload")
        skipped += 1
        continue

    old_uid = payload.get("userId", "")
    if old_uid == CURRENT_USER_ID:
        print(f"  OK   {name}: already correct")
        skipped += 1
        continue

    if old_uid not in STALE_IDS:
        print(f"  SKIP {name}: unknown userId={old_uid}")
        skipped += 1
        continue

    # Patch the payload
    payload["userId"] = CURRENT_USER_ID
    new_input = json.dumps(payload)

    if DRY_RUN:
        print(f"  DRY  {name}: {old_uid} -> {CURRENT_USER_ID}")
        patched += 1
        continue

    # Build update kwargs — must include all required fields
    update_kwargs = {
        "Name": name,
        "GroupName": SCHEDULE_GROUP,
        "ScheduleExpression": sched["ScheduleExpression"],
        "FlexibleTimeWindow": sched["FlexibleTimeWindow"],
        "Target": {
            **sched["Target"],
            "Input": new_input,
        },
    }
    # Optional fields that must be forwarded if present
    if sched.get("ScheduleExpressionTimezone"):
        update_kwargs["ScheduleExpressionTimezone"] = sched["ScheduleExpressionTimezone"]
    if sched.get("State"):
        update_kwargs["State"] = sched["State"]
    if sched.get("StartDate"):
        update_kwargs["StartDate"] = sched["StartDate"]
    if sched.get("EndDate"):
        update_kwargs["EndDate"] = sched["EndDate"]
    if sched.get("Description"):
        update_kwargs["Description"] = sched["Description"]
    if sched.get("ActionAfterCompletion"):
        update_kwargs["ActionAfterCompletion"] = sched["ActionAfterCompletion"]

    try:
        scheduler.update_schedule(**update_kwargs)
        print(f"  FIXED {name}: {old_uid} -> {CURRENT_USER_ID}")
        patched += 1
    except Exception as e:
        print(f"  ERROR {name}: {e}")
        errors += 1

print(f"\nDone: {patched} patched, {skipped} skipped, {errors} errors")
