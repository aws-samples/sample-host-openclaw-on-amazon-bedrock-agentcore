"""AgentCore Runtime V2 identity enrollment and channel ownership.

Registers a user's profiles, session route, channel mapping, and durable actor
owner in one DynamoDB transaction. Retained ownership blocks re-registration;
linking another channel preserves the canonical actor that owns runtime state.
Callers supply the identity table, a transaction client, and a verified V2
target for enrollment or the existing route for channel linking.
"""

import hashlib
from decimal import Decimal
import re
import time
import uuid

from runtime_routing import RouteUnavailable, _condition, route_from_record


def _attributes(item):
    values = {}
    for key, value in item.items():
        if isinstance(value, str):
            values[key] = {"S": value}
        elif type(value) is int or isinstance(value, Decimal):
            values[key] = {"N": str(value)}
        else:
            raise ValueError(f"Unsupported enrollment attribute: {key}")
    return values


def _put(table_name, item, condition="attribute_not_exists(PK)", values=None):
    put = {"TableName": table_name, "Item": _attributes(item)}
    if condition:
        put["ConditionExpression"] = condition
    if values:
        put["ExpressionAttributeValues"] = _attributes(values)
    return {"Put": put}


def read_actor_owner(table, actor_id):
    owner = table.get_item(
        Key={"PK": f"ACTOR#{actor_id}", "SK": "OWNER"}, ConsistentRead=True,
    ).get("Item")
    if owner and (not owner.get("userId") or owner.get("platformVersion") != "V2"
                  or owner.get("actorId") != actor_id
                  or isinstance(owner.get("ownerGeneration"), bool)
                  or not isinstance(owner.get("ownerGeneration"), (int, Decimal))
                  or owner["ownerGeneration"] < 1
                  or owner["ownerGeneration"] != int(owner["ownerGeneration"])):
        raise RouteUnavailable("Invalid durable V2 actor ownership; reconciliation required")
    return owner


# Most pages the legacy ownership scan reads before failing closed. Without a
# Limit each page is up to 1 MB, so this covers about 1 GB of identity table.
_RECONCILE_SCAN_MAX_PAGES = 1000


def _derived_user_id(actor_id):
    return f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:16]}"


def _deterministic_actor_residue(table, actor_id):
    """Probe the derived USER# partition for residue of a prior V2 enrollment.

    These O(1) reads find V2 residue without depending on table size; the
    filtered scan that follows only has to find legacy (random user id) records.
    """
    partition = f"USER#{_derived_user_id(actor_id)}"
    backref = table.get_item(
        Key={"PK": partition, "SK": f"CHANNEL#{actor_id}"}, ConsistentRead=True,
    ).get("Item")
    if backref:
        return True
    profile = table.get_item(
        Key={"PK": partition, "SK": "PROFILE"}, ConsistentRead=True,
    ).get("Item") or {}
    if profile.get("enrollmentActorId") == actor_id:
        return True
    session = table.get_item(
        Key={"PK": partition, "SK": "SESSION"}, ConsistentRead=True,
    ).get("Item") or {}
    return session.get("actorId") == actor_id


def reconcile_actor_owner(table, actor_id):
    owner = read_actor_owner(table, actor_id)
    if owner:
        return owner
    # Fast deterministic path: residue of a prior V2 enrollment for this actor
    # always lives under the derived USER#<sha256(actor_id)> partition and is
    # retrievable with O(1) get_item calls that do not depend on table size.
    if _deterministic_actor_residue(table, actor_id):
        raise RouteUnavailable("Actor has retained ownership; reconciliation required")
    cursor = None
    # Missing CHANNEL is not evidence that the actor's state namespace is unused.
    for _ in range(_RECONCILE_SCAN_MAX_PAGES):
        request = {
            "ConsistentRead": True,
            "FilterExpression": "begins_with(#pk, :users) AND (#sk = :backref OR #actor = :actor OR #enrolled = :actor)",
            "ExpressionAttributeNames": {
                "#pk": "PK", "#sk": "SK", "#actor": "actorId", "#enrolled": "enrollmentActorId",
            },
            "ExpressionAttributeValues": {":users": "USER#", ":backref": f"CHANNEL#{actor_id}", ":actor": actor_id},
        }
        if cursor:
            request["ExclusiveStartKey"] = cursor
        response = table.scan(**request)
        if response.get("Items"):
            raise RouteUnavailable("Actor has retained ownership; reconciliation required")
        cursor = response.get("LastEvaluatedKey")
        if not cursor:
            return None
    raise RouteUnavailable("Legacy actor ownership scan incomplete; registration blocked")


def enroll_new_user(table, client, table_name, channel, channel_user_id, display_name, target):
    if not isinstance(target, dict) or target.get("platformVersion") != "V2":
        raise RouteUnavailable("Registration requires a verified V2 runtime target")
    if (any(not isinstance(target.get(key), str) for key in (
            "runtimeArn", "endpointName", "runtimeVersion", "imageDigest", "configurationHash"))
            or target["endpointName"] == "DEFAULT"
            or not re.fullmatch(r"[1-9][0-9]*", target["runtimeVersion"])
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", target["imageDigest"])
            or not re.fullmatch(r"[a-f0-9]{64}", target["configurationHash"])):
        raise RouteUnavailable("Invalid V2 registration target")
    actor_id = f"{channel}:{channel_user_id}"
    if reconcile_actor_owner(table, actor_id):
        raise RouteUnavailable("Actor has retained ownership; repair its channel mapping before registration")
    user_id = f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:16]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    session = {
        "PK": f"USER#{user_id}", "SK": "SESSION", "sessionId": f"ses_{uuid.uuid4().hex}",
        "runtimeArn": target["runtimeArn"], "endpointName": target["endpointName"],
        "generation": 1, "actorId": actor_id,
        "platformVersion": "V2", "routeStatus": "ACTIVE",
        "createdAt": now, "lastActivity": now,
    }
    session.update({key: target[key] for key in ("runtimeVersion", "imageDigest", "configurationHash")})
    route_from_record(session, user_id)
    records = [
        {
            "PK": f"CHANNEL#{actor_id}", "SK": "PROFILE", "userId": user_id,
            "channel": channel, "channelUserId": channel_user_id,
            "displayName": display_name or channel_user_id, "boundAt": now,
        },
        {
            "PK": f"USER#{user_id}", "SK": "PROFILE", "userId": user_id,
            "createdAt": now, "displayName": display_name or channel_user_id,
            "runtimeEnrollment": "V2", "enrollmentActorId": actor_id,
        },
        session,
        {
            "PK": f"USER#{user_id}", "SK": f"CHANNEL#{actor_id}",
            "channel": channel, "channelUserId": channel_user_id, "boundAt": now,
        },
        {
            "PK": f"ACTOR#{actor_id}", "SK": "OWNER", "userId": user_id,
            "actorId": actor_id, "platformVersion": "V2", "ownerGeneration": 1,
        },
    ]
    try:
        client.transact_write_items(
            ClientRequestToken=uuid.uuid4().hex,
            TransactItems=[_put(table_name, record) for record in records],
        )
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        winner = table.get_item(
            Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"}, ConsistentRead=True,
        ).get("Item")
        if winner and winner.get("userId"):
            owner = read_actor_owner(table, actor_id)
            winner_session = table.get_item(
                Key={"PK": f"USER#{winner['userId']}", "SK": "SESSION"}, ConsistentRead=True,
            ).get("Item", {})
            if not owner or owner["userId"] != winner["userId"]:
                raise RouteUnavailable("Concurrent registration ownership requires reconciliation") from error
            route_from_record(winner_session, winner["userId"])
            return winner["userId"], False
        raise RouteUnavailable("Canonical identity already exists or registration could not complete") from error
    return user_id, True


def authorize_channel_actor(table, route, user_id, actor_id, channel):
    """Authorize the delivery identity without changing the runtime state owner."""
    if (route.platform_version != "V2" or route.user_id != user_id
            or not isinstance(actor_id, str) or not actor_id.startswith(f"{channel}:")):
        raise RouteUnavailable("Channel identity does not match the runtime user")
    if actor_id == route.actor_id:
        return
    owner = read_actor_owner(table, actor_id)
    mapping = table.get_item(
        Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"}, ConsistentRead=True,
    ).get("Item", {})
    backref = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{actor_id}"}, ConsistentRead=True,
    ).get("Item", {})
    if (not owner or owner["userId"] != user_id or mapping.get("userId") != user_id
            or backref.get("runtimeActorId") != route.actor_id
            or owner.get("runtimeActorId") != route.actor_id):
        raise RouteUnavailable("Channel is not explicitly bound to the canonical runtime actor")


def link_actor(table_name, client, actor_id, user_id, current, owner, code, display_name, route):
    if (route.user_id != user_id or route.platform_version != "V2"
            or (current and current.get("userId") != user_id)
            or (owner and (owner.get("userId") != user_id or owner.get("platformVersion") != "V2"))
            or bool(current) != bool(owner)):
        raise RouteUnavailable("Actor ownership cannot be transferred between canonical users")
    channel, channel_user_id = actor_id.split(":", 1)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    channel_record = {
        "PK": f"CHANNEL#{actor_id}", "SK": "PROFILE", "userId": user_id,
        "channel": channel, "channelUserId": channel_user_id,
        "displayName": display_name or channel_user_id, "boundAt": now,
    }
    generation = int(owner["ownerGeneration"]) + 1 if owner else 1
    owner_record = {
        "PK": f"ACTOR#{actor_id}", "SK": "OWNER", "userId": user_id, "actorId": actor_id,
        "platformVersion": "V2", "ownerGeneration": generation, "runtimeActorId": route.actor_id,
    }
    transactions = [
        _put(table_name, channel_record, "userId = :user" if current else "attribute_not_exists(PK)",
             {":user": current["userId"]} if current else None),
        _put(table_name, owner_record,
             "userId = :user AND ownerGeneration = :generation AND platformVersion = :platform"
             if owner else "attribute_not_exists(PK)",
             {":user": user_id, ":generation": generation - 1, ":platform": "V2"}
             if owner else None),
        _put(table_name, {
            "PK": f"USER#{user_id}", "SK": f"CHANNEL#{actor_id}",
            "channel": channel, "channelUserId": channel_user_id, "boundAt": now,
            "runtimeActorId": route.actor_id,
        }, condition=("runtimeActorId = :actor" if actor_id != route.actor_id else "attribute_exists(PK)")
            if current else "attribute_not_exists(PK)",
            values={":actor": route.actor_id} if current and actor_id != route.actor_id else None),
        {"Delete": {
            "TableName": table_name, "Key": _attributes({"PK": f"BIND#{code}", "SK": "BIND"}),
            "ConditionExpression": "userId = :user AND #ttl >= :now AND runtimeActorId = :actor AND generation = :generation AND sessionId = :session",
            "ExpressionAttributeNames": {"#ttl": "ttl"},
            "ExpressionAttributeValues": _attributes({
                ":user": user_id, ":now": int(time.time()), ":actor": route.actor_id,
                ":generation": route.generation, ":session": route.session_id,
            }),
        }},
        {"ConditionCheck": {
            "TableName": table_name, "Key": _attributes({"PK": f"USER#{user_id}", "SK": "PROFILE"}),
            "ConditionExpression": "runtimeEnrollment = :platform AND enrollmentActorId = :actor",
            "ExpressionAttributeValues": _attributes({":platform": "V2", ":actor": route.actor_id}),
        }},
    ]
    condition, names, values = _condition(route)
    transactions.append({"ConditionCheck": {
        "TableName": table_name, "Key": _attributes(route.key),
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names, "ExpressionAttributeValues": _attributes(values),
    }})
    if actor_id != route.actor_id:
        transactions.append({"ConditionCheck": {
            "TableName": table_name,
            "Key": _attributes({"PK": f"ACTOR#{route.actor_id}", "SK": "OWNER"}),
            "ConditionExpression": "userId = :user AND platformVersion = :platform AND actorId = :actor",
            "ExpressionAttributeValues": _attributes({":user": user_id, ":platform": "V2", ":actor": route.actor_id}),
        }})
    if not current:
        separate_user = f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:16]}"
        if separate_user != user_id:
            for sort_key in ("PROFILE", "SESSION"):
                transactions.append({"ConditionCheck": {
                    "TableName": table_name,
                    "Key": _attributes({"PK": f"USER#{separate_user}", "SK": sort_key}),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }})
    client.transact_write_items(ClientRequestToken=uuid.uuid4().hex, TransactItems=transactions)
