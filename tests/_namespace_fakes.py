"""Stateful AWS fakes for the namespace migration tools (no AWS calls).

scripts/namespace_backfill.py, scripts/namespace_cutover.py and scripts/delete_user_data.py
run against these fakes end to end. Every request is checked against botocore's service model
(tests/test_delete_user_data.py validate), so a wrong parameter name or type fails the test.

- Table evaluates the condition, update, filter and key expressions it is sent (the grammar
  subset the tools use) and, as DynamoDB does, rejects an expression attribute name or value
  that no expression uses and a reserved word used without a placeholder.
- S3 is a versioned bucket. CopyObject gives the copy a new ETag, as SSE-KMS does, so a tool
  cannot tell a copy from its source by ETag. LastModified and the Date header come from the
  shared Clock, which moves on one second after every write.
- Scheduler, Secrets Manager, Cognito, CloudWatch Logs and AgentCore keep what the tools did.

All IDs are synthetic.
"""

import copy
import datetime
import email.utils
import hashlib
import json
import re
import uuid
from decimal import Decimal
from urllib.parse import quote

from boto3.dynamodb.types import TypeSerializer

from tests.test_delete_user_data import (
    ARN, BUCKET, POOL, TABLE, TOKENS, Cfn, client_error, validate,
)

_SERIALIZE = TypeSerializer().serialize
MUTATING = {"put_item", "update_item", "delete_item", "copy_object", "delete_objects", "create_schedule",
            "update_schedule", "delete_schedule", "create_secret", "put_secret_value", "restore_secret",
            "delete_secret", "admin_delete_user", "delete_log_stream", "stop_runtime_session"}
GROUP = "openclaw-cron"
CRON_LAMBDA = "arn:aws:lambda:us-west-2:123456789012:function:openclaw-cron"
SCHEDULER_ROLE = "arn:aws:iam::123456789012:role/openclaw-scheduler"
START = datetime.datetime(2026, 11, 5, 9, 0, tzinfo=datetime.timezone.utc)


class Log(list):
    def mutating(self):
        return [call for call in self if call[1] in MUTATING]


class Clock:
    """now(), monotonic() and sleep() for a tool run; later() schedules a callback in fake time."""

    def __init__(self, start=START):
        self.moment, self.ticks, self.events = start, 0.0, []

    def now(self):
        return self.moment

    def monotonic(self):
        return self.ticks

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        target = self.ticks + seconds
        while True:
            due = sorted((when, n) for n, (when, _) in enumerate(self.events) if when <= target)
            if not due:
                break
            when, index = due[0]
            _, callback = self.events.pop(index)
            self._move(when)
            callback()
        self._move(target)

    def _move(self, ticks):
        if ticks > self.ticks:
            self.moment += datetime.timedelta(seconds=ticks - self.ticks)
            self.ticks = ticks

    def later(self, seconds, callback):
        self.events.append((self.ticks + seconds, callback))

    def second(self):
        return self.moment.replace(microsecond=0)


class Stub:
    service = ""

    def __init__(self, log):
        self.log, self.failures, self.hooks = log, {}, {}

    def fail_on(self, method, error, after=0):
        """The call to method after `after` successful ones raises error, once."""
        self.failures[method] = [after, error]

    def on(self, method, callback):
        """Run callback once, after the next successful call to method."""
        self.hooks[method] = callback

    def __getattribute__(self, name):
        attribute = object.__getattribute__(self, name)
        hooks = object.__getattribute__(self, "__dict__").get("hooks") or {}
        if name not in hooks or not callable(attribute):
            return attribute

        def call(**params):
            result = attribute(**params)
            hook = hooks.pop(name, None)
            if hook:
                hook()
            return result
        return call

    def _call(self, method, params, detail=None):
        validate(self.service, method, params)
        self.log.append((self.service, method, detail, None))
        if method in self.failures:
            if self.failures[method][0] == 0:
                raise self.failures.pop(method)[1]
            self.failures[method][0] -= 1


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"<>|<=|>=|[=<>(),+\-]|#[A-Za-z0-9_]+|:[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*")
_CLAUSES = ("SET", "REMOVE", "ADD", "DELETE")
_FUNCTIONS = ("attribute_exists", "attribute_not_exists", "begins_with", "contains")
# A subset of DynamoDB's reserved words: the ones an attribute of these tools could be called.
RESERVED = {"STATE", "STATUS", "NAME", "SOURCE", "FROM", "TO", "DATE", "KEY", "VALUE", "VALUES", "USER", "USERS",
            "TOKEN", "DATA", "COUNT", "OWNER", "SESSION", "TTL", "TIMESTAMP", "TYPE", "SIZE", "PATH", "ROLLBACK",
            "RECORD", "PLAN", "COPY", "TIME", "ZONE", "REGION", "POOL", "BUCKET", "OLD", "NEW", "LOG", "SET"}
_MISSING = object()


def _invalid(why):
    return client_error("ValidationException", why)


def _tokens(text):
    tokens, position = [], 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(text, position)
        if not match:
            raise _invalid(f"cannot parse {text[position:]!r}")
        tokens.append(match.group())
        position = match.end()
    return tokens


def _number(value):
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _compare(op, left, right):
    if left is _MISSING or right is _MISSING:
        return False
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if not ((_number(left) and _number(right)) or (isinstance(left, str) and isinstance(right, str))):
        return False
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]


class _Parser:
    def __init__(self, request, text):
        self.names = request.get("ExpressionAttributeNames") or {}
        self.values = request.get("ExpressionAttributeValues") or {}
        self.tokens, self.index = _tokens(text), 0

    def peek(self, offset=0):
        position = self.index + offset
        return self.tokens[position] if position < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token.upper() != expected):
            raise _invalid(f"expected {expected or 'a token'}, got {token!r}")
        self.index += 1
        return token

    def keyword(self, word):
        if (self.peek() or "").upper() == word:
            self.index += 1
            return True
        return False

    def done(self):
        if self.peek() is not None:
            raise _invalid(f"unexpected {self.peek()!r}")

    def name(self, token):
        if token.startswith("#"):
            if token not in self.names:
                raise _invalid(f"undefined name {token}")
            return self.names[token]
        if token.upper() in RESERVED or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            raise _invalid(f"reserved word or bad attribute name {token!r}")
        return token

    def value(self, token):
        if token not in self.values:
            raise _invalid(f"undefined value {token}")
        return self.values[token]

    # Conditions: OR < AND < NOT < predicates.
    def condition(self, item):
        result = self.conjunction(item)
        while self.keyword("OR"):
            right = self.conjunction(item)
            result = result or right
        return result

    def conjunction(self, item):
        result = self.negation(item)
        while self.keyword("AND"):
            right = self.negation(item)
            result = result and right
        return result

    def negation(self, item):
        if self.keyword("NOT"):
            return not self.negation(item)
        return self.predicate(item)

    def predicate(self, item):
        if self.peek() == "(":
            self.take("(")
            result = self.condition(item)
            self.take(")")
            return result
        function = self.peek()
        if function in _FUNCTIONS and self.peek(1) == "(":
            self.take()
            self.take("(")
            current = item.get(self.name(self.take()), _MISSING)
            if function in ("attribute_exists", "attribute_not_exists"):
                self.take(")")
                return (current is not _MISSING) == (function == "attribute_exists")
            self.take(",")
            operand = self.operand(item)
            self.take(")")
            if function == "begins_with":
                return isinstance(current, str) and isinstance(operand, str) and current.startswith(operand)
            return isinstance(current, (set, frozenset, list, str)) and operand in current
        left = self.operand(item)
        if self.keyword("BETWEEN"):
            low = self.operand(item)
            self.take("AND")
            high = self.operand(item)
            return _compare("<=", low, left) and _compare("<=", left, high)
        if self.keyword("IN"):
            self.take("(")
            options = [self.operand(item)]
            while self.peek() == ",":
                self.take(",")
                options.append(self.operand(item))
            self.take(")")
            return left is not _MISSING and left in options
        op = self.take()
        if op not in ("=", "<>", "<", "<=", ">", ">="):
            raise _invalid(f"unexpected {op!r}")
        return _compare(op, left, self.operand(item))

    def operand(self, item):
        token = self.take()
        if token.startswith(":"):
            return self.value(token)
        if token == "size" and self.peek() == "(":
            self.take("(")
            current = item.get(self.name(self.take()), _MISSING)
            self.take(")")
            return _MISSING if current is _MISSING else len(current)
        return item.get(self.name(token), _MISSING)

    # Updates: every value is read from the item as it was before the update.
    def update(self, item):
        if self.peek() is None:
            raise _invalid("empty update expression")
        actions = []
        while self.peek() is not None:
            clause = self.take().upper()
            if clause not in _CLAUSES:
                raise _invalid(f"unexpected {clause!r}")
            while True:
                path = self.name(self.take())
                if path in ("PK", "SK"):
                    raise _invalid("cannot update a key attribute")
                if clause == "SET":
                    self.take("=")
                    actions.append((clause, path, self.set_value(item)))
                elif clause == "REMOVE":
                    actions.append((clause, path, None))
                else:
                    actions.append((clause, path, self.value(self.take())))
                if self.peek() != ",":
                    break
                self.take(",")
        result = copy.deepcopy(item)
        for clause, path, value in actions:
            if clause == "SET":
                result[path] = value
            elif clause == "REMOVE":
                result.pop(path, None)
            elif clause == "ADD" and isinstance(value, (set, frozenset)):
                result[path] = set(result.get(path) or set()) | set(value)
            elif clause == "ADD":
                result[path] = (result.get(path) or 0) + value
            else:
                remaining = set(result.get(path) or set()) - set(value)
                if remaining:
                    result[path] = remaining
                else:
                    result.pop(path, None)
        return result

    def set_value(self, item):
        left = self.set_operand(item)
        if self.peek() in ("+", "-"):
            op = self.take()
            right = self.set_operand(item)
            if not (_number(left) and _number(right)):
                raise _invalid("arithmetic on a non-number")
            return left + right if op == "+" else left - right
        return left

    def set_operand(self, item):
        token = self.take()
        if token == "if_not_exists":
            self.take("(")
            path = self.name(self.take())
            self.take(",")
            default = self.set_operand(item)
            self.take(")")
            return copy.deepcopy(item[path]) if path in item else default
        if token.startswith(":"):
            return copy.deepcopy(self.value(token))
        path = self.name(token)
        if path not in item:
            raise _invalid("the provided expression refers to an attribute that does not exist in the item")
        return copy.deepcopy(item[path])


def _as_read(value):
    """What boto3's resource layer returns: numbers as Decimal."""
    if _number(value):
        return Decimal(value)
    if isinstance(value, dict):
        return {k: _as_read(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_read(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return {_as_read(v) for v in value}
    return value


class Table:
    """A DynamoDB table through the boto3 Table resource API."""

    def __init__(self, name, log, page=100):
        self.name, self.log, self.page = name, log, page
        self.items, self.failures = {}, {}

    def put(self, **item):
        self.items[(item["PK"], item["SK"])] = copy.deepcopy(item)

    def get(self, pk, sk):
        return self.items.get((pk, sk))

    def fail_on(self, method, key, error, after=0):
        self.failures[(method, key)] = [after, error]

    def _call(self, method, kwargs):
        low = dict(kwargs, TableName=self.name)
        for field in ("Key", "ExclusiveStartKey", "Item", "ExpressionAttributeValues"):
            if field in low:
                low[field] = {k: _SERIALIZE(v) for k, v in low[field].items()}
        validate("dynamodb", method, low)
        texts = " ".join(kwargs.get(k) or "" for k in ("ConditionExpression", "UpdateExpression",
                                                       "KeyConditionExpression", "FilterExpression"))
        placeholders = {t for t in _tokens(texts) if t[0] in "#:"}
        names = set(kwargs.get("ExpressionAttributeNames") or {})
        values = set(kwargs.get("ExpressionAttributeValues") or {})
        if (names | values) - placeholders:
            raise _invalid(f"unused expression names or values {sorted((names | values) - placeholders)}")
        if placeholders - names - values:
            raise _invalid(f"undefined placeholders {sorted(placeholders - names - values)}")
        source = kwargs.get("Key") or kwargs.get("Item")
        key = (source["PK"], source["SK"]) if source else None
        self.log.append(("dynamodb", method, self.name, key))
        failure = self.failures.get((method, key))
        if failure:
            if failure[0] == 0:
                raise self.failures.pop((method, key))[1]
            failure[0] -= 1
        return key

    @staticmethod
    def _holds(kwargs, text, item):
        if not text:
            return True
        parser = _Parser(kwargs, text)
        result = parser.condition(item)
        parser.done()
        return result

    def get_item(self, **kwargs):
        key = self._call("get_item", kwargs)
        item = self.items.get(key)
        return {"Item": _as_read(item)} if item else {}

    def put_item(self, **kwargs):
        key = self._call("put_item", kwargs)
        if not self._holds(kwargs, kwargs.get("ConditionExpression"), self.items.get(key) or {}):
            raise client_error("ConditionalCheckFailedException")
        self.items[key] = copy.deepcopy(kwargs["Item"])
        return {}

    def update_item(self, **kwargs):
        key = self._call("update_item", kwargs)
        current = self.items.get(key)
        if not self._holds(kwargs, kwargs.get("ConditionExpression"), current or {}):
            raise client_error("ConditionalCheckFailedException")
        parser = _Parser(kwargs, kwargs["UpdateExpression"])
        updated = parser.update(current or {"PK": key[0], "SK": key[1]})
        parser.done()
        self.items[key] = updated
        return {"Attributes": _as_read(updated)} if kwargs.get("ReturnValues") == "ALL_NEW" else {}

    def delete_item(self, **kwargs):
        key = self._call("delete_item", kwargs)
        if not self._holds(kwargs, kwargs.get("ConditionExpression"), self.items.get(key) or {}):
            raise client_error("ConditionalCheckFailedException")
        self.items.pop(key, None)
        return {}

    def _page(self, keys, kwargs):
        cursor = kwargs.get("ExclusiveStartKey")
        start = keys.index((cursor["PK"], cursor["SK"])) + 1 if cursor else 0
        page = keys[start:start + kwargs.get("Limit", self.page)]
        more = start + len(page) < len(keys)
        return page, ({"LastEvaluatedKey": {"PK": page[-1][0], "SK": page[-1][1]}} if more else {})

    def query(self, **kwargs):
        self._call("query", kwargs)
        assert kwargs["KeyConditionExpression"] == "PK = :pk"
        keys = sorted(k for k in self.items if k[0] == kwargs["ExpressionAttributeValues"][":pk"])
        page, more = self._page(keys, kwargs)
        return {"Items": [_as_read(self.items[k]) for k in page], **more}

    def scan(self, **kwargs):
        self._call("scan", kwargs)
        page, more = self._page(sorted(self.items), kwargs)
        items = [self.items[k] for k in page if self._holds(kwargs, kwargs.get("FilterExpression"), self.items[k])]
        return {"Items": [_as_read(item) for item in items], **more}


# ---------------------------------------------------------------------------
# S3, Scheduler, Secrets Manager, Cognito, CloudWatch Logs, AgentCore
# ---------------------------------------------------------------------------

class S3(Stub):
    """A versioned bucket. A write is stamped with the clock's second, then the clock moves on."""
    service = "s3"

    def __init__(self, log, clock, page=1000):
        super().__init__(log)
        self.clock, self.page, self.versions, self.counter, self.copies = clock, page, {}, 0, []

    def _version(self, key, body, size=None, marker=False):
        self.counter += 1
        self.versions.setdefault(key, []).append({
            "VersionId": f"v{self.counter:06d}", "ETag": f'"{uuid.uuid4().hex}"', "Body": body,
            "Size": len(body) if size is None else size, "LastModified": self.clock.second(), "marker": marker})
        self.clock.advance(1)

    def put(self, key, body="data", size=None):
        """A write by the bridge or a skill: a new current version."""
        self._version(key, body, size)

    def remove(self, key):
        """A delete without a version ID: a delete marker."""
        self._version(key, None, 0, marker=True)

    def current(self, key):
        versions = self.versions.get(key) or []
        return versions[-1] if versions and not versions[-1]["marker"] else None

    def tree(self, prefix):
        """{rest of key: body} of the current objects under prefix."""
        return {key[len(prefix):]: self.current(key)["Body"] for key in sorted(self.versions)
                if key.startswith(prefix) and self.current(key)}

    def count(self, prefix):
        return sum(len(v) for k, v in self.versions.items() if k.startswith(prefix))

    def list_objects_v2(self, **params):
        self._call("list_objects_v2", params)
        keys = [k for k in sorted(self.versions) if k.startswith(params.get("Prefix", "")) and self.current(k)]
        start = int(params.get("ContinuationToken") or 0)
        page = keys[start:start + self.page]
        response = {"Contents": [{"Key": k, "ETag": self.current(k)["ETag"], "Size": self.current(k)["Size"],
                                  "LastModified": self.current(k)["LastModified"], "StorageClass": "STANDARD"}
                                 for k in page],
                    "IsTruncated": start + len(page) < len(keys), "KeyCount": len(page),
                    "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {
                        "date": email.utils.format_datetime(self.clock.second(), usegmt=True)}}}
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + len(page))
        return response

    def copy_object(self, **params):
        source = params["CopySource"]
        # botocore turns a dict CopySource into "bucket/key" before it validates the request.
        self._call("copy_object", dict(params, CopySource=f"{source['Bucket']}/{quote(source['Key'])}"),
                   params["Key"])
        assert source["Bucket"] == params["Bucket"] == BUCKET
        current = self.current(source["Key"])
        if current is None:
            raise client_error("NoSuchKey")
        if current["Size"] > 5 * 1024 ** 3:
            raise client_error("InvalidRequest")
        self._version(params["Key"], current["Body"], current["Size"])
        self.copies.append((source["Key"], params["Key"]))
        return {"CopyObjectResult": {"ETag": self.versions[params["Key"]][-1]["ETag"]}}

    def list_object_versions(self, **params):
        self._call("list_object_versions", params)
        rows = [(k, v) for k in sorted(self.versions) if k.startswith(params.get("Prefix", ""))
                for v in self.versions[k]]
        start = 0
        if "KeyMarker" in params:
            start = next(n for n, (k, v) in enumerate(rows)
                         if (k, v["VersionId"]) == (params["KeyMarker"], params.get("VersionIdMarker"))) + 1
        page = rows[start:start + self.page]
        response = {"Versions": [{"Key": k, "VersionId": v["VersionId"], "Size": v["Size"]}
                                 for k, v in page if not v["marker"]],
                    "DeleteMarkers": [{"Key": k, "VersionId": v["VersionId"]} for k, v in page if v["marker"]],
                    "IsTruncated": start + len(page) < len(rows)}
        if response["IsTruncated"]:
            response.update(NextKeyMarker=page[-1][0], NextVersionIdMarker=page[-1][1]["VersionId"])
        return response

    def delete_objects(self, **params):
        self._call("delete_objects", params)
        objects = params["Delete"]["Objects"]
        assert len(objects) <= 1000 and all(o.get("VersionId") for o in objects)
        doomed = {(o["Key"], o["VersionId"]) for o in objects}
        for key in list(self.versions):
            self.versions[key] = [v for v in self.versions[key] if (key, v["VersionId"]) not in doomed]
            if not self.versions[key]:
                del self.versions[key]
        return {}


class Scheduler(Stub):
    service = "scheduler"

    def __init__(self, log, page=2):
        super().__init__(log)
        self.schedules, self.page = {}, page

    def add(self, name, payload, state="ENABLED", expression="cron(0 9 * * ? *)", timezone="Europe/Berlin"):
        self.schedules[name] = {
            "Name": name, "GroupName": GROUP, "State": state, "ScheduleExpression": expression,
            "ScheduleExpressionTimezone": timezone, "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": CRON_LAMBDA, "RoleArn": SCHEDULER_ROLE, "Input": json.dumps(payload)},
            "Description": "OpenClaw cron: synthetic"}

    def list_schedules(self, **params):
        self._call("list_schedules", params)
        assert params["GroupName"] == GROUP
        names = sorted(n for n in self.schedules if n.startswith(params.get("NamePrefix", "")))
        start = int(params.get("NextToken") or 0)
        response = {"Schedules": [{"Name": n, "GroupName": GROUP, "State": self.schedules[n]["State"]}
                                  for n in names[start:start + self.page]]}
        if start + self.page < len(names):
            response["NextToken"] = str(start + self.page)
        return response

    def get_schedule(self, **params):
        self._call("get_schedule", params)
        if params["Name"] not in self.schedules:
            raise client_error("ResourceNotFoundException")
        return {**copy.deepcopy(self.schedules[params["Name"]]),
                "Arn": f"arn:aws:scheduler:us-west-2:123456789012:schedule/{GROUP}/{params['Name']}"}

    def create_schedule(self, **params):
        self._call("create_schedule", params, params["Name"])
        if params["Name"] in self.schedules:
            raise client_error("ConflictException")
        self.schedules[params["Name"]] = copy.deepcopy(params)
        return {"ScheduleArn": f"arn:aws:scheduler:us-west-2:123456789012:schedule/{GROUP}/{params['Name']}"}

    def update_schedule(self, **params):
        """UpdateSchedule replaces the whole schedule: a field left out is reset."""
        self._call("update_schedule", params, params["Name"])
        if params["Name"] not in self.schedules:
            raise client_error("ResourceNotFoundException")
        self.schedules[params["Name"]] = copy.deepcopy(params)
        return {}

    def delete_schedule(self, **params):
        self._call("delete_schedule", params, params["Name"])
        if self.schedules.pop(params["Name"], None) is None:
            raise client_error("ResourceNotFoundException")
        return {}


class Secrets(Stub):
    service = "secretsmanager"

    def __init__(self, log, clock):
        super().__init__(log)
        self.clock, self.secrets = clock, {}

    def add(self, name, value, tags=None, pending=False, kms=None):
        self.secrets[name] = {"ARN": f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{name}-"
                                     + hashlib.sha256(name.encode()).hexdigest()[:6],
                              "Name": name, "SecretString": value, "Tags": tags or []}
        if kms:
            self.secrets[name]["KmsKeyId"] = kms
        if pending:
            self.secrets[name]["DeletedDate"] = self.clock.now()

    def _find(self, secret_id):
        found = [s for s in self.secrets.values() if secret_id in (s["Name"], s["ARN"])]
        if not found:
            raise client_error("ResourceNotFoundException")
        return found[0]

    def live(self, name):
        secret = self.secrets.get(name)
        return secret and not secret.get("DeletedDate") and secret["SecretString"]

    def list_secrets(self, **params):
        self._call("list_secrets", params)
        prefix = params["Filters"][0]["Values"][0].lower()
        rows = [{k: copy.deepcopy(v) for k, v in s.items() if k != "SecretString"}
                for n, s in sorted(self.secrets.items()) if n.lower().startswith(prefix)
                and (params.get("IncludePlannedDeletion") or "DeletedDate" not in s)]
        return {"SecretList": rows}

    def describe_secret(self, **params):
        self._call("describe_secret", params)
        return {k: copy.deepcopy(v) for k, v in self._find(params["SecretId"]).items() if k != "SecretString"}

    def get_secret_value(self, **params):
        self._call("get_secret_value", params)
        secret = self._find(params["SecretId"])
        if secret.get("DeletedDate"):
            raise client_error("InvalidRequestException")
        return {"ARN": secret["ARN"], "Name": secret["Name"], "SecretString": secret["SecretString"],
                "VersionId": "synthetic"}

    def create_secret(self, **params):
        self._call("create_secret", params, params["Name"])
        if params["Name"] in self.secrets:
            raise client_error("InvalidRequestException" if self.secrets[params["Name"]].get("DeletedDate")
                               else "ResourceExistsException")
        self.add(params["Name"], params["SecretString"], params.get("Tags"), kms=params.get("KmsKeyId"))
        return {"ARN": self.secrets[params["Name"]]["ARN"], "Name": params["Name"]}

    def put_secret_value(self, **params):
        self._call("put_secret_value", params, params["SecretId"])
        secret = self._find(params["SecretId"])
        if secret.get("DeletedDate"):
            raise client_error("InvalidRequestException")
        secret["SecretString"] = params["SecretString"]
        return {"ARN": secret["ARN"], "Name": secret["Name"]}

    def restore_secret(self, **params):
        self._call("restore_secret", params, params["SecretId"])
        self._find(params["SecretId"]).pop("DeletedDate", None)
        return {}

    def delete_secret(self, **params):
        self._call("delete_secret", params, params["SecretId"])
        secret = self._find(params["SecretId"])
        if params.get("ForceDeleteWithoutRecovery"):
            del self.secrets[secret["Name"]]
        elif secret.get("DeletedDate"):
            raise client_error("InvalidRequestException")
        else:
            secret["DeletedDate"] = self.clock.now()
        return {}


class Cognito(Stub):
    service = "cognito-idp"

    def __init__(self, log):
        super().__init__(log)
        self.users = set()

    def admin_delete_user(self, **params):
        self._call("admin_delete_user", params, params["Username"])
        assert params["UserPoolId"] == POOL
        if params["Username"] not in self.users:
            raise client_error("UserNotFoundException")
        self.users.discard(params["Username"])
        return {}


class Logs(Stub):
    service = "logs"

    def __init__(self, log, page=2):
        super().__init__(log)
        self.streams, self.page = set(), page

    def describe_log_streams(self, **params):
        self._call("describe_log_streams", params)
        assert params["logGroupName"] == "/openclaw/container"
        names = sorted(n for n in self.streams if n.startswith(params["logStreamNamePrefix"]))
        start = int(params.get("nextToken") or 0)
        response = {"logStreams": [{"logStreamName": n} for n in names[start:start + self.page]]}
        if start + self.page < len(names):
            response["nextToken"] = str(start + self.page)
        return response

    def delete_log_stream(self, **params):
        self._call("delete_log_stream", params, params["logStreamName"])
        self.streams.discard(params["logStreamName"])
        return {}


class AgentCore(Stub):
    service = "bedrock-agentcore"

    def __init__(self, log):
        super().__init__(log)
        self.stopped = []

    def stop_runtime_session(self, **params):
        self._call("stop_runtime_session", params)
        self.stopped.append(params)
        return {}


class Aws:
    def __init__(self):
        self.log, self.clock = Log(), Clock()
        self.cfn = Cfn(self.log)
        self.identity, self.tokens = Table(TABLE, self.log), Table(TOKENS, self.log)
        self.s3, self.scheduler = S3(self.log, self.clock), Scheduler(self.log)
        self.secrets, self.cognito = Secrets(self.log, self.clock), Cognito(self.log)
        self.logs, self.agentcore = Logs(self.log), AgentCore(self.log)

    def table(self, name):
        return {TABLE: self.identity, TOKENS: self.tokens}[name]

    def state(self):
        return copy.deepcopy((self.identity.items, self.tokens.items, self.s3.versions, self.scheduler.schedules,
                              self.secrets.secrets, self.cognito.users, self.logs.streams))


def enrol(aws, actor, user, namespace=None, sessions=1):
    """A V2 user whose runtime actor is actor; namespace is stored on SESSION when given."""
    t = aws.identity
    t.put(PK=f"CHANNEL#{actor}", SK="PROFILE", userId=user, channel=actor.split(":")[0])
    t.put(PK=f"ACTOR#{actor}", SK="OWNER", userId=user, actorId=actor, platformVersion="V2", ownerGeneration=1)
    t.put(PK=f"USER#{user}", SK="PROFILE", userId=user, runtimeEnrollment="V2", enrollmentActorId=actor)
    session = {"PK": f"USER#{user}", "SK": "SESSION", "sessionId": "ses_" + hashlib.md5(user.encode()).hexdigest(),
               "runtimeArn": ARN, "endpointName": "release_1", "generation": sessions, "platformVersion": "V2",
               "routeStatus": "ACTIVE", "actorId": actor}
    if namespace:
        session["namespace"] = namespace
        t.put(PK=f"NS#{namespace}", SK="OWNER", userId=user, createdAt="2026-11-01T00:00:00Z", source="backfill")
    t.put(**session)
    t.put(PK=f"USER#{user}", SK=f"CHANNEL#{actor}", channel=actor.split(":")[0])
    t.put(PK=f"ALLOW#{actor}", SK="ALLOW", channelKey=actor)


def schedule_payload(user, actor, schedule_id):
    return {"userId": user, "actorId": actor, "channelActorId": actor, "channel": actor.split(":")[0],
            "channelTarget": actor.split(":")[1], "message": "synthetic reminder", "scheduleId": schedule_id,
            "scheduleName": f"Schedule {schedule_id}", "scheduledTime": "<aws.scheduler.scheduled-time>"}
