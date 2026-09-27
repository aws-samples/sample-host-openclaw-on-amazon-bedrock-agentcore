#!/usr/bin/env python3
"""Create missing DynamoDB CRON# ownership records for orphaned schedules.

After userId rotation, some schedules lost their CRON# records (they were
keyed under now-deleted stale userIds). This script reads each schedule's
payload and expression from EventBridge, and creates CRON# records under
the current userId so the ownership check passes.

Usage:
    python3 scripts/fix-missing-cron-records.py \\
        --namespace telegram_<telegram_id> --user-id user_<16 hex> [--dry-run]

--namespace / --user-id may also be set via OPENCLAW_NAMESPACE /
OPENCLAW_USER_ID.
"""
import json
import time

from _ns_args import parse_args

ARGS = parse_args(__doc__.splitlines()[0])

import boto3  # noqa: E402

REGION = "ap-southeast-2"
SCHEDULE_GROUP = "openclaw-cron"
NAMESPACE_PREFIX = f"openclaw-{ARGS.namespace}-"
CURRENT_USER_ID = ARGS.user_id
TELEGRAM_ID = ARGS.telegram_id
TABLE_NAME = "openclaw-identity"

DRY_RUN = ARGS.dry_run

scheduler = boto3.client("scheduler", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(TABLE_NAME)

# Get all schedule names
paginator = scheduler.get_paginator("list_schedules")
all_names = []
for page in paginator.paginate(GroupName=SCHEDULE_GROUP):
    for s in page["Schedules"]:
        if s["Name"].startswith(NAMESPACE_PREFIX):
            all_names.append(s["Name"])

# Find which are missing CRON# records
missing = []
for name in sorted(all_names):
    sid = name.split("-")[-1]
    resp = table.get_item(Key={"PK": f"USER#{CURRENT_USER_ID}", "SK": f"CRON#{sid}"})
    if "Item" not in resp:
        missing.append(name)

print(f"Total schedules: {len(all_names)}, missing CRON# records: {len(missing)}")

created = 0
errors = 0

for name in missing:
    sid = name.split("-")[-1]
    try:
        sched = scheduler.get_schedule(Name=name, GroupName=SCHEDULE_GROUP)
    except Exception as e:
        print(f"  ERROR reading {name}: {e}")
        errors += 1
        continue

    payload = json.loads(sched["Target"].get("Input", "{}"))
    expression = sched.get("ScheduleExpression", "")
    timezone = sched.get("ScheduleExpressionTimezone", "UTC")
    state = sched.get("State", "ENABLED")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    item = {
        "PK": f"USER#{CURRENT_USER_ID}",
        "SK": f"CRON#{sid}",
        "scheduleId": sid,
        "scheduleName": payload.get("scheduleName", sid),
        "actorId": payload.get("actorId", f"telegram:{TELEGRAM_ID}"),
        "channel": payload.get("channel", "telegram"),
        "channelTarget": payload.get("channelTarget", TELEGRAM_ID),
        "message": payload.get("message", ""),
        "expression": expression,
        "timezone": timezone,
        "enabled": state == "ENABLED",
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }

    if DRY_RUN:
        print(f"  DRY  CRON#{sid}: {item['scheduleName']} ({expression})")
        created += 1
        continue

    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        print(f"  CREATED CRON#{sid}: {item['scheduleName']}")
        created += 1
    except Exception as e:
        if "ConditionalCheckFailedException" in str(e):
            print(f"  EXISTS CRON#{sid}: already created (race condition)")
        else:
            print(f"  ERROR CRON#{sid}: {e}")
            errors += 1

print(f"\nDone: {created} created, {errors} errors")
