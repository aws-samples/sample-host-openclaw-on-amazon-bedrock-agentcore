"""Shared per-user AgentCore Runtime routing and invocation admission.

Treats the DynamoDB SESSION record as the canonical V2 endpoint, session, and
actor binding. Consistent reads and conditional updates reject missing or
changed routes instead of silently choosing a replacement. Admission tokens
track active callers and prevent freezing a route while work is outstanding;
callers retain tokens for unknown outcomes until explicit reconciliation.

The route also names the user's storage namespace (the S3 prefix and the
secret, schedule and session names built from it). A SESSION without a
`namespace` attribute uses the legacy namespace, the runtime actor with ':'
replaced by '_'. A stored namespace is used only when the `NS#<namespace>` /
OWNER record, which lives outside the user's partition, names the same user.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)


class RouteUnavailable(RuntimeError):
    pass


class RouteMigrating(RouteUnavailable):
    """The user's storage is being moved to another namespace (scripts/namespace_cutover.py)."""


# A stored namespace is either the legacy value or "ns_" + 26 base32 characters
# (128 random bits, scripts/namespace_cutover.py).
OPAQUE_NAMESPACE = re.compile(r"ns_[a-z2-7]{26}")


def legacy_namespace(actor_id):
    """The namespace of a route that stores none: the runtime actor with ':' as '_'."""
    return actor_id.replace(":", "_")


class RuntimeAdmission:
    def __init__(self):
        self._retained = False

    @property
    def retained(self):
        return self._retained

    def retain(self):
        self._retained = True


def is_unknown_outcome(response):
    if not isinstance(response, dict):
        return False
    execution = response.get("execution")
    return (response.get("status") == "unknown_outcome"
            or (isinstance(execution, dict) and execution.get("phase") == "unknown_outcome"))


_ROUTE_FIELDS = (
    "sessionId", "runtimeArn", "endpointName", "generation",
    "platformVersion", "routeStatus", "actorId", "runtimeVersion", "imageDigest", "configurationHash",
    "namespace",
)
_TARGET_FIELDS = ("runtimeArn", "endpointName", "generation", "platformVersion")
_RUNTIME_ARN = re.compile(r"arn:[a-z0-9-]+:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[A-Za-z0-9_-]+")
_ENDPOINT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")


@dataclass(frozen=True)
class RuntimeRoute:
    user_id: str
    runtime_arn: str
    endpoint_name: str
    session_id: str
    generation: int
    platform_version: str
    actor_id: str
    namespace: str
    snapshot: dict = field(repr=False, compare=False)

    def __str__(self):
        return self.session_id

    @property
    def key(self):
        return {"PK": f"USER#{self.user_id}", "SK": "SESSION"}

    @property
    def namespace_stored(self):
        """True when SESSION carries the namespace instead of deriving it from the actor."""
        return "namespace" in self.snapshot

    def invocation(self):
        return {
            "agentRuntimeArn": self.runtime_arn,
            "qualifier": self.endpoint_name,
            "runtimeSessionId": self.session_id,
        }

    def payload_context(self):
        return {"runtimeSessionId": self.session_id, "routingGeneration": self.generation,
                "namespace": self.namespace}


def route_from_record(item, user_id):
    if item.get("PK") != f"USER#{user_id}" or item.get("SK") != "SESSION":
        raise RouteUnavailable("Session owner mismatch; reconciliation required")
    session_id = item.get("sessionId")
    if (not isinstance(session_id, str) or not 33 <= len(session_id) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id)):
        raise RouteUnavailable("Invalid AgentCore Runtime session ID; reconciliation required")
    if item.get("routeStatus") == "MIGRATING":
        raise RouteMigrating("User storage is being migrated; try again shortly")
    if item.get("routeStatus") != "ACTIVE":
        raise RouteUnavailable("AgentCore Runtime route is not active; reconciliation required")
    if not all(key in item for key in _TARGET_FIELDS):
        raise RouteUnavailable("Incomplete AgentCore Runtime route; reconciliation required")
    arn = item["runtimeArn"]
    endpoint = item["endpointName"]
    generation = item["generation"]
    platform = item["platformVersion"]
    actor = item.get("actorId", "")
    if not isinstance(arn, str) or not _RUNTIME_ARN.fullmatch(arn):
        raise RouteUnavailable("Invalid AgentCore Runtime ARN; reconciliation required")
    if not isinstance(endpoint, str) or not _ENDPOINT_NAME.fullmatch(endpoint) or endpoint == "DEFAULT":
        raise RouteUnavailable("AgentCore Runtime qualifier must be an endpoint name; reconciliation required")
    if (isinstance(generation, bool) or not isinstance(generation, (int, Decimal))
            or not 1 <= generation <= 2**53 - 1 or generation != int(generation)):
        raise RouteUnavailable("Invalid routing generation; reconciliation required")
    if platform != "V2":
        raise RouteUnavailable("AgentCore Runtime platform must be V2; reconciliation required")
    if not isinstance(actor, str) or not actor:
        raise RouteUnavailable("V2 requires a bound actor; reconciliation required")
    namespace = item.get("namespace", legacy_namespace(actor))
    if (not isinstance(namespace, str)
            or (namespace != legacy_namespace(actor) and not OPAQUE_NAMESPACE.fullmatch(namespace))):
        raise RouteUnavailable("Invalid stored namespace; reconciliation required")
    return RuntimeRoute(user_id, arn, endpoint, session_id, int(generation), platform,
                        actor, namespace, {key: item[key] for key in _ROUTE_FIELDS if key in item})


def _conditional_failure(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def resolve_route(table, user_id):
    key = {"PK": f"USER#{user_id}", "SK": "SESSION"}
    item = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise RouteUnavailable("Canonical AgentCore Runtime route is missing; reconciliation required")
    route = route_from_record(item, user_id)
    if route.namespace_stored:
        verify_namespace_owner(table, route.namespace, user_id)
    return route


def verify_namespace_owner(table, namespace, user_id):
    """Fail closed unless NS#<namespace>/OWNER names user_id.

    The owner record is outside the user's USER#<id> partition and is written
    only by the operator tools (scripts/namespace_backfill.py and
    scripts/namespace_cutover.py), so it, not SESSION, decides the namespace.
    """
    owner = table.get_item(Key={"PK": f"NS#{namespace}", "SK": "OWNER"}, ConsistentRead=True).get("Item")
    if not owner or owner.get("userId") != user_id:
        raise RouteUnavailable("Stored namespace is not owned by this user; reconciliation required")


def _condition(route):
    terms, names, values = [], {}, {}
    for key in _ROUTE_FIELDS:
        alias = f"#{key}"
        names[alias] = key
        if key in route.snapshot:
            terms.append(f"{alias} = :{key}")
            values[f":{key}"] = route.snapshot[key]
        else:
            terms.append(f"attribute_not_exists({alias})")
    return " AND ".join(terms), names, values


def _update(table, **kwargs):
    try:
        return table.update_item(**kwargs)
    except Exception as error:
        if _conditional_failure(error):
            raise RouteUnavailable("AgentCore Runtime route changed or has active callers") from error
        raise


@contextmanager
def admit_route(table, route, user_id, actor_id):
    if route.platform_version != "V2" or route.user_id != user_id or route.actor_id != actor_id:
        raise RouteUnavailable("AgentCore Runtime route identity mismatch")
    condition, names, values = _condition(route)
    names["#inFlight"] = "inFlight"
    names["#admissions"] = "activeAdmissions"
    token = uuid.uuid4().hex
    token_values = {":token": token, ":tokens": {token}}
    try:
        table.update_item(
            Key=route.key,
            UpdateExpression="SET lastActivity = :now ADD #inFlight :delta, #admissions :tokens",
            ConditionExpression=f"({condition}) AND (attribute_not_exists(#admissions) OR NOT contains(#admissions, :token))",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                **values, **token_values, ":delta": 1,
                ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except Exception as error:
        if not _conditional_failure(error):
            raise
        current = table.get_item(Key=route.key, ConsistentRead=True).get("Item", {})
        if (token not in current.get("activeAdmissions", set())
                or {key: current[key] for key in _ROUTE_FIELDS if key in current} != route.snapshot):
            raise RouteUnavailable("AgentCore Runtime route changed or admission could not be confirmed") from error
    admission = RuntimeAdmission()
    try:
        yield admission
    except Exception as error:
        if getattr(error, "retain_admission", False):
            admission.retain()
        raise
    finally:
        # Unknown outcomes and killed Lambdas keep their token until reconciliation.
        if not admission.retained:
            try:
                table.update_item(
                    Key=route.key,
                    UpdateExpression="ADD #inFlight :delta DELETE #admissions :tokens",
                    ConditionExpression=f"({condition}) AND #inFlight >= :one AND contains(#admissions, :token)",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues={**values, **token_values, ":delta": -1, ":one": 1},
                )
            except Exception:
                # Never replace a known result or its delivery ownership.
                logger.exception("Admission release was not confirmed; reconcile token %s before migration", token)


def freeze_route(table, route):
    """Close new invocation admission; this does not stop background/S3 writers."""
    condition, names, values = _condition(route)
    names["#inFlight"] = "inFlight"
    names["#admissions"] = "activeAdmissions"
    _update(
        table, Key=route.key, UpdateExpression="SET #routeStatus = :draining",
        ConditionExpression=f"({condition}) AND (attribute_not_exists(#inFlight) OR #inFlight = :zero) AND attribute_not_exists(#admissions)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":draining": "DRAINING", ":zero": 0},
    )
