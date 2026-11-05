"""Tests for scripts/delete_user_data.py with every AWS client stubbed (no AWS calls).

The stubs keep state, so a test can stop a run part-way and re-run it against what the first
run left. Every request is checked against botocore's service model, as botocore.stub.Stubber
does, so a wrong parameter name or type fails the test. DynamoDB uses
tests.test_runtime_routing.FakeTable, which evaluates the route conditions freeze_route sends.

All IDs below are synthetic.
"""

import copy
import datetime
import hashlib
import importlib.util
import io
import json
from decimal import Decimal
from pathlib import Path

import botocore.session
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore import xform_name
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

from tests.test_runtime_routing import FakeTable

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("delete_user_data", ROOT / "scripts" / "delete_user_data.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def derived(actor):
    return f"user_{hashlib.sha256(actor.encode()).hexdigest()[:16]}"


RUNTIME = "telegram:100000001"
LINKED = "slack:U0SYNTH001"
NS = "telegram_100000001"
USER = derived(RUNTIME)
OTHER = "telegram:1000000012"  # its namespace starts with ours: prefixes must be exact
OTHER_NS = "telegram_1000000012"
OTHER_USER = derived(OTHER)
GROUP = "telegram_group:-1001234567890"
POOL = "us-west-2_EXAMPLE"
TABLE, TOKENS = "openclaw-identity", "openclaw-token-usage"
BUCKET = "openclaw-user-files-123456789012-us-west-2"
ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/openclaw_agent-abc123"
MUTATING = {"delete_item", "update_item", "put_item", "batch_write_item", "transact_write_items", "delete_schedule",
            "delete_objects", "delete_secret", "admin_delete_user", "delete_log_stream", "stop_runtime_session"}
TODAY = datetime.date(2026, 11, 5)

_MODELS = {}
_SERIALIZE = TypeSerializer().serialize


def validate(service, method, params):
    """Reject a request botocore would reject (unknown parameter, wrong type, missing field)."""
    if service not in _MODELS:
        _MODELS[service] = botocore.session.get_session().get_service_model(service)
    model = _MODELS[service]
    operation = {xform_name(name): name for name in model.operation_names}[method]
    report = ParamValidator().validate(params, model.operation_model(operation).input_shape)
    assert not report.has_errors(), report.generate_report()


def client_error(code, operation="Call"):
    return ClientError({"Error": {"Code": code, "Message": "synthetic"}}, operation)


class Log(list):
    def mutating(self):
        return [call for call in self if call[1] in MUTATING]


class Table(FakeTable):
    """FakeTable plus query, the group scan, conditional deletes and the PROFILE update the tool sends."""

    def __init__(self, name, log, page=100):
        super().__init__()
        self.name, self.log, self.page, self.fail = name, log, page, {}

    def _call(self, method, kwargs):
        low = dict(kwargs, TableName=self.name)
        for key in ("Key", "ExclusiveStartKey"):
            if key in low:
                low[key] = {k: _SERIALIZE(v) for k, v in low[key].items()}
        if "ExpressionAttributeValues" in low:
            low["ExpressionAttributeValues"] = {k: _SERIALIZE(v) for k, v in low["ExpressionAttributeValues"].items()}
        validate("dynamodb", method, low)
        key = tuple(kwargs["Key"].values()) if "Key" in kwargs else None
        self.log.append(("dynamodb", method, self.name, key))
        error = self.fail.pop((method, key), None)
        if error:
            raise error

    def put(self, **item):
        self.items[(item["PK"], item["SK"])] = item

    def get_item(self, **kwargs):
        self._call("get_item", kwargs)
        return super().get_item(**kwargs)

    def query(self, **kwargs):
        self._call("query", kwargs)
        assert kwargs["KeyConditionExpression"] == "PK = :pk" and kwargs.get("ConsistentRead") is True
        keys = sorted(k for k in self.items if k[0] == kwargs["ExpressionAttributeValues"][":pk"])
        cursor = kwargs.get("ExclusiveStartKey")
        start = keys.index((cursor["PK"], cursor["SK"])) + 1 if cursor else 0
        page = keys[start:start + self.page]
        response = {"Items": [copy.deepcopy(self.items[k]) for k in page]}
        if start + len(page) < len(keys):
            response["LastEvaluatedKey"] = {"PK": page[-1][0], "SK": page[-1][1]}
        return response

    def scan(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        if ":group" not in values:
            return super().scan(**kwargs)
        self._call("scan", kwargs)
        actors = {v for k, v in values.items() if k.startswith(":a")}
        return {"Items": [copy.deepcopy(item) for key, item in sorted(self.items.items())
                          if key[0].startswith(values[":group"]) and item.get("addedBy") in actors]}

    def update_item(self, **kwargs):
        if "accessStatus" not in kwargs["UpdateExpression"]:
            self._call("update_item", kwargs)
            return super().update_item(**kwargs)
        self._call("update_item", kwargs)
        item = self.items.get(tuple(kwargs["Key"].values()))
        if item is None:
            raise client_error("ConditionalCheckFailedException")
        item["accessStatus"] = kwargs["ExpressionAttributeValues"][":disabled"]
        for name in ("disabledAt", "deletionStartedAt"):
            item.setdefault(name, kwargs["ExpressionAttributeValues"][":now"])
        return {}

    def delete_item(self, **kwargs):
        self._call("delete_item", kwargs)
        key = tuple(kwargs["Key"].values())
        if kwargs.get("ConditionExpression"):
            assert kwargs["ConditionExpression"] == "userId = :user"
            if (self.items.get(key) or {}).get("userId") != kwargs["ExpressionAttributeValues"][":user"]:
                raise client_error("ConditionalCheckFailedException")
        self.items.pop(key, None)
        return {}


class Stub:
    service = ""

    def __init__(self, log):
        self.log, self.fail = log, {}

    def _call(self, method, params, detail=None):
        validate(self.service, method, params)
        self.log.append((self.service, method, detail, None))
        error = self.fail.pop(method, None)
        if error:
            raise error


class Cfn(Stub):
    service = "cloudformation"

    def __init__(self, log, suffix=""):
        super().__init__(log)
        tail = f"-{suffix}" if suffix else ""
        self.stacks = {f"OpenClawRouter{tail}": {"IdentityTableName": TABLE + tail},
                       f"OpenClawTokenMonitoring{tail}": {"TokenUsageTableName": TOKENS + tail},
                       f"OpenClawAgentCore{tail}": {"UserFilesBucketName": BUCKET},
                       f"OpenClawSecurity{tail}": {"CognitoUserPoolId": POOL}}

    def describe_stacks(self, **params):
        self._call("describe_stacks", params, params["StackName"])
        if params["StackName"] not in self.stacks:
            raise client_error("ValidationError")
        outputs = [{"OutputKey": k, "OutputValue": v} for k, v in self.stacks[params["StackName"]].items()]
        return {"Stacks": [{"StackName": params["StackName"], "Outputs": outputs}]}


class S3(Stub):
    service = "s3"

    def __init__(self, log, page=1000):
        super().__init__(log)
        self.entries, self.page, self.batches, self.after_delete = [], page, [], None

    def add(self, key, versions=1, marker=False, size=10):
        for _ in range(versions):
            self.entries.append({"Key": key, "VersionId": f"v{len(self.entries):06d}", "Size": size, "marker": False})
        if marker:
            self.entries.append({"Key": key, "VersionId": f"v{len(self.entries):06d}", "marker": True})

    def keys(self, prefix):
        return [e for e in self.entries if e["Key"].startswith(prefix)]

    def list_object_versions(self, **params):
        self._call("list_object_versions", params)
        rows = sorted((e for e in self.entries if e["Key"].startswith(params.get("Prefix", ""))),
                      key=lambda e: (e["Key"], e["VersionId"]))
        start = 0
        if "KeyMarker" in params:
            start = next(i for i, e in enumerate(rows)
                         if (e["Key"], e["VersionId"]) == (params["KeyMarker"], params.get("VersionIdMarker"))) + 1
        page = rows[start:start + self.page]
        response = {"Versions": [{"Key": e["Key"], "VersionId": e["VersionId"], "Size": e["Size"]}
                                 for e in page if not e["marker"]],
                    "DeleteMarkers": [{"Key": e["Key"], "VersionId": e["VersionId"]} for e in page if e["marker"]],
                    "IsTruncated": start + len(page) < len(rows)}
        if response["IsTruncated"]:
            response.update(NextKeyMarker=page[-1]["Key"], NextVersionIdMarker=page[-1]["VersionId"])
        return response

    def delete_objects(self, **params):
        self._call("delete_objects", params)
        objects = params["Delete"]["Objects"]
        assert len(objects) <= 1000, "S3 rejects more than 1,000 keys per DeleteObjects call"
        assert all(o.get("VersionId") for o in objects), "a delete without VersionId only adds a marker"
        self.batches.append(len(objects))
        doomed = {(o["Key"], o["VersionId"]) for o in objects}
        self.entries = [e for e in self.entries if (e["Key"], e["VersionId"]) not in doomed]
        if self.after_delete:
            hook, self.after_delete = self.after_delete, None
            hook(self)
        return {}


class Scheduler(Stub):
    service = "scheduler"

    def __init__(self, log, page=2):
        super().__init__(log)
        self.schedules, self.page, self.groups = {}, page, set()

    def add(self, name, actor):
        self.schedules[name] = {"Name": name, "Target": {"Input": json.dumps({"actorId": actor})}}

    def list_schedules(self, **params):
        self._call("list_schedules", params)
        self.groups.add(params["GroupName"])
        names = sorted(n for n in self.schedules if n.startswith(params.get("NamePrefix", "")))
        start = int(params.get("NextToken", "0"))
        response = {"Schedules": [{"Name": n} for n in names[start:start + self.page]]}
        if start + self.page < len(names):
            response["NextToken"] = str(start + self.page)
        return response

    def get_schedule(self, **params):
        self._call("get_schedule", params)
        if params["Name"] not in self.schedules:
            raise client_error("ResourceNotFoundException")
        return copy.deepcopy(self.schedules[params["Name"]])

    def delete_schedule(self, **params):
        self._call("delete_schedule", params, params["Name"])
        if self.schedules.pop(params["Name"], None) is None:
            raise client_error("ResourceNotFoundException")
        return {}


class Secrets(Stub):
    service = "secretsmanager"

    def __init__(self, log):
        super().__init__(log)
        self.secrets, self.deletes = {}, []

    def add(self, name, pending=False):
        arn = f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{name}-AbCdEf"
        self.secrets[name] = {"ARN": arn, "Name": name}
        if pending:
            self.secrets[name]["DeletedDate"] = datetime.datetime(2026, 11, 1)

    def list_secrets(self, **params):
        self._call("list_secrets", params)
        prefix = params["Filters"][0]["Values"][0].lower()
        rows = [copy.deepcopy(s) for n, s in sorted(self.secrets.items()) if n.lower().startswith(prefix)
                and (params.get("IncludePlannedDeletion") or "DeletedDate" not in s)]
        return {"SecretList": rows}

    def describe_secret(self, **params):
        self._call("describe_secret", params)
        found = [s for s in self.secrets.values() if params["SecretId"] in (s["Name"], s["ARN"])]
        if not found:
            raise client_error("ResourceNotFoundException")
        return copy.deepcopy(found[0])

    def delete_secret(self, **params):
        self._call("delete_secret", params)
        self.deletes.append(params)
        name = next(n for n, s in self.secrets.items() if s["ARN"] == params["SecretId"])
        if params.get("ForceDeleteWithoutRecovery"):
            del self.secrets[name]
        else:
            self.secrets[name]["DeletedDate"] = datetime.datetime(2026, 11, 5)
        return {}


class Cognito(Stub):
    service = "cognito-idp"

    def __init__(self, log):
        super().__init__(log)
        self.users = set()

    def admin_delete_user(self, **params):
        self._call("admin_delete_user", params)
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
        start = int(params.get("nextToken", "0"))
        response = {"logStreams": [{"logStreamName": n} for n in names[start:start + self.page]]}
        if start + self.page < len(names):
            response["nextToken"] = str(start + self.page)
        return response

    def delete_log_stream(self, **params):
        self._call("delete_log_stream", params)
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
    def __init__(self, suffix=""):
        self.log = Log()
        self.cfn = Cfn(self.log, suffix)
        tail = f"-{suffix}" if suffix else ""
        self.tables = {TABLE + tail: Table(TABLE + tail, self.log), TOKENS + tail: Table(TOKENS + tail, self.log)}
        self.s3, self.scheduler, self.secrets = S3(self.log), Scheduler(self.log), Secrets(self.log)
        self.cognito, self.logs, self.agentcore = Cognito(self.log), Logs(self.log), AgentCore(self.log)
        self.identity, self.tokens = self.tables[TABLE + tail], self.tables[TOKENS + tail]

    def table(self, name):
        return self.tables[name]

    def state(self):
        return copy.deepcopy((self.identity.items, self.tokens.items, self.s3.entries, self.scheduler.schedules,
                              self.secrets.secrets, self.cognito.users, self.logs.streams))


def enrol(aws, actor, user, linked=()):
    t = aws.identity
    t.put(PK=f"CHANNEL#{actor}", SK="PROFILE", userId=user, channel=actor.split(":")[0])
    t.put(PK=f"ACTOR#{actor}", SK="OWNER", userId=user, actorId=actor, platformVersion="V2", ownerGeneration=1)
    t.put(PK=f"USER#{user}", SK="PROFILE", userId=user, runtimeEnrollment="V2", enrollmentActorId=actor)
    t.put(PK=f"USER#{user}", SK="SESSION", sessionId="ses_" + hashlib.md5(user.encode()).hexdigest(),
          runtimeArn=ARN, endpointName="release_1", generation=1, platformVersion="V2", routeStatus="ACTIVE",
          actorId=actor)
    t.put(PK=f"USER#{user}", SK=f"CHANNEL#{actor}", channel=actor.split(":")[0])
    t.put(PK=f"ALLOW#{actor}", SK="ALLOW", channelKey=actor)
    for other in linked:
        t.put(PK=f"CHANNEL#{other}", SK="PROFILE", userId=user, channel=other.split(":")[0])
        t.put(PK=f"ACTOR#{other}", SK="OWNER", userId=user, actorId=other, platformVersion="V2", ownerGeneration=1,
              runtimeActorId=actor)
        t.put(PK=f"USER#{user}", SK=f"CHANNEL#{other}", channel=other.split(":")[0], runtimeActorId=actor)
        t.put(PK=f"ALLOW#{other}", SK="ALLOW", channelKey=other)


def world(suffix=""):
    """Our user (Telegram, linked to Slack) and another user whose namespace starts with ours."""
    aws = Aws(suffix)
    enrol(aws, RUNTIME, USER, linked=[LINKED])
    enrol(aws, OTHER, OTHER_USER)
    t = aws.identity
    for sid in ("a1b2c3d4", "e5f6a7b8"):
        t.put(PK=f"USER#{USER}", SK=f"CRON#{sid}", scheduleId=sid, ebName=f"openclaw-{NS}-{sid}")
        aws.scheduler.add(f"openclaw-{NS}-{sid}", RUNTIME)
    aws.scheduler.add(f"openclaw-{NS}-0badf00d", RUNTIME)  # no CRON# record left
    aws.scheduler.add(f"openclaw-{OTHER_NS}-aaaa0000", OTHER)
    t.put(PK=f"USER#{USER}", SK="BIND_POINTER", userId=USER, code="ABCDEFGH23456723")
    t.put(PK="BIND#ABCDEFGH23456723", SK="BIND", userId=USER, runtimeActorId=RUNTIME)
    t.put(PK=f"ALLOW#{GROUP}", SK="ALLOW", channelKey=GROUP, addedBy=RUNTIME, groupTitle="Synthetic group")
    for ns in (NS, OTHER_NS):
        aws.s3.add(f"{ns}/.openclaw/state.db", versions=3)
        aws.s3.add(f"{ns}/_uploads/img_1.jpg", versions=1, marker=True)
        aws.s3.add(f"_system/{ns}/AGENTS.md", versions=2)
        aws.secrets.add(f"openclaw/user/{ns}/OPENAI_API_KEY")
        aws.secrets.add(f"openclaw/cognito-user/{POOL}/{ns}")
        aws.logs.streams |= {f"{ns}-1730000000000", f"{ns}-1730000100000"}
    aws.logs.streams.add(f"{USER}-1730000200000")
    aws.cognito.users |= {RUNTIME, OTHER}
    for actor, rows in ((RUNTIME, 3), (USER, 1), (OTHER, 2)):
        for n in range(rows):
            aws.tokens.put(PK=f"USER#{actor}", SK=f"DATE#2026-11-0{n + 1}#CHANNEL#telegram#SESSION#s", actorId=actor)
    # Daily usage items and a limit override (per_user_daily_cost_usd), outside USER#<id>.
    for uid in (USER, OTHER_USER):
        t.put(PK=f"USAGE#{uid}", SK="DAY#2026-11-05", costUsd=Decimal("1.5"), calls=Decimal(3))
    t.put(PK=f"USAGE#{USER}", SK="CAP", dailyCostUsd=Decimal(2), setAt="2026-11-01T00:00:00Z")
    return aws


def run(aws, *args, suffix=""):
    out = io.StringIO()
    code = tool.main(["--region", "us-west-2", "--env-suffix", suffix, *args], aws=aws, out=out, today=TODAY)
    return code, out.getvalue()


def ours(aws):
    """Everything of our user that is left anywhere."""
    left = [k for k in aws.identity.items if USER in k[0] or k[0] in (
        f"CHANNEL#{RUNTIME}", f"ACTOR#{RUNTIME}", f"CHANNEL#{LINKED}", f"ACTOR#{LINKED}", f"ALLOW#{RUNTIME}",
        f"ALLOW#{LINKED}", "BIND#ABCDEFGH23456723")]
    left += aws.s3.keys(f"{NS}/") + aws.s3.keys(f"_system/{NS}/")
    left += [n for n in aws.scheduler.schedules if n.startswith(f"openclaw-{NS}-")]
    left += [n for n, s in aws.secrets.secrets.items() if our_secret(n) and not s.get("DeletedDate")]
    left += [u for u in aws.cognito.users if u == RUNTIME]
    left += [k for k in aws.tokens.items if k[0] in (f"USER#{RUNTIME}", f"USER#{USER}")]
    left += [s for s in aws.logs.streams if s.startswith((f"{NS}-", f"{USER}-"))]
    return left


def our_secret(name):
    return name.endswith(f"/{NS}") or f"/{NS}/" in name


def theirs(aws):
    """The other user's data, which must survive."""
    return ([k for k in aws.identity.items if OTHER_USER in k[0] or OTHER in k[0]], aws.s3.keys(f"{OTHER_NS}/"),
            aws.s3.keys(f"_system/{OTHER_NS}/"), [n for n in aws.scheduler.schedules if OTHER_NS in n],
            [n for n in aws.secrets.secrets if OTHER_NS in n], OTHER in aws.cognito.users,
            [k for k in aws.tokens.items if k[0] == f"USER#{OTHER}"], [s for s in aws.logs.streams if OTHER_NS in s])


def step_of(call):
    """The tool step a mutating call belongs to."""
    service, method, where, key = call
    if service == "dynamodb" and where == TOKENS:
        return 8
    if service == "dynamodb":
        pk, sk = key
        if pk.startswith("ALLOW#") or sk == "PROFILE" and method == "update_item":
            return 2
        if sk == "SESSION" and method == "update_item":
            return 3
        return 4 if sk.startswith("CRON#") else 10
    return {"stop_runtime_session": 3, "delete_schedule": 4, "delete_objects": 5, "delete_secret": 6,
            "admin_delete_user": 7, "delete_log_stream": 9}[method]


# ---------------------------------------------------------------------------


def test_dry_run_makes_no_mutating_call_and_lists_every_step():
    aws = world()
    before = aws.state()
    code, out = run(aws, "--actor", RUNTIME)
    assert code == 0, out
    assert aws.log.mutating() == []
    assert aws.state() == before
    for step in range(1, 12):
        assert f"[{step}] " in out, step
    assert "would delete 3 schedules, then 2 CRON# records" in out
    assert "would delete 6 object versions (60 bytes) and 1 delete marker" in out
    # The 2 CRON# records belong to step 4, so step 10 lists the other 5 partition items.
    assert "would delete 5 items under USER#<id> and 5 CHANNEL#, ACTOR#, BIND# or NS# records" in out
    assert "would delete 2 usage items under USAGE#<id>" in out
    assert "would schedule 2 secrets for deletion with a 7-day recovery window" in out
    assert "would delete 4 rows" in out and "would delete 3 log streams" in out
    assert "AUDIT" not in out


def test_output_names_the_user_by_internal_id_only():
    for args in (("--actor", LINKED), ("--actor", LINKED, "--apply")):
        aws = world()
        code, out = run(aws, *args)
        assert code == 0, out
        assert USER in out
        for secret in ("100000001", "U0SYNTH001", NS, OTHER_NS, POOL, BUCKET, "ABCDEFGH23456723"):
            assert secret not in out, secret


def test_apply_runs_the_steps_in_order_and_identity_last():
    aws = world()
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    steps = [step_of(call) for call in aws.log.mutating()]
    assert steps == sorted(steps)
    assert set(steps) == set(range(2, 11))
    assert aws.log.mutating()[-1][1:] == ("delete_item", TABLE, (f"USER#{USER}", "PROFILE"))
    assert ours(aws) == []
    assert all(theirs(aws))
    assert aws.agentcore.stopped == [{"agentRuntimeArn": ARN, "qualifier": "release_1",
                                      "runtimeSessionId": "ses_" + hashlib.md5(USER.encode()).hexdigest()}]
    assert out.rstrip().endswith(f"AUDIT: {USER} data deleted 2026-11-05")


def test_usage_items_are_deleted_in_the_identity_step_and_only_this_users():
    aws = world()
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert "deleted 2 usage items under USAGE#<id>" in out
    assert not [key for key in aws.identity.items if key[0] == f"USAGE#{USER}"]
    assert (f"USAGE#{OTHER_USER}", "DAY#2026-11-05") in aws.identity.items
    deletes = [call for call in aws.log.mutating() if call[3] and call[3][0] == f"USAGE#{USER}"]
    assert sorted(call[3][1] for call in deletes) == ["CAP", "DAY#2026-11-05"]
    assert {step_of(call) for call in deletes} == {10}


def test_any_linked_actor_or_the_user_id_selects_the_same_user():
    for args in (("--actor", LINKED), ("--user-id", USER)):
        aws = world()
        code, out = run(aws, *args, "--apply")
        assert code == 0, out
        assert ours(aws) == [] and all(theirs(aws))


@pytest.mark.parametrize("failure", [
    ("s3", "delete_objects", client_error("InternalError"), "[5] FAILED: InternalError"),
    ("secrets", "delete_secret", client_error("ThrottlingException"), "[6] FAILED: ThrottlingException"),
    ("identity", ("delete_item", (f"ACTOR#{RUNTIME}", "OWNER")), client_error("ProvisionedThroughputExceededException"),
     "[10] FAILED: ProvisionedThroughputExceededException"),
])
def test_a_rerun_after_a_partial_failure_finishes_the_job(failure):
    aws = world()
    where, method, error, message = failure
    getattr(aws, where).fail[method] = error
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 1 and message in out, out
    assert "re-run the same command to continue" in out
    assert "AUDIT" not in out
    profile = aws.identity.items[(f"USER#{USER}", "PROFILE")]
    assert profile["accessStatus"] == "DISABLED"  # revoked before anything else
    code, out = run(aws, "--user-id", USER, "--apply")
    assert code == 0, out
    assert ours(aws) == [] and all(theirs(aws))
    code, out = run(aws, "--user-id", USER, "--apply")
    assert code == 0 and "nothing to delete" in out


def test_a_rerun_by_actor_after_the_identity_step_was_interrupted():
    aws = world()
    aws.identity.fail[("delete_item", (f"ACTOR#{RUNTIME}", "OWNER"))] = client_error("InternalServerError")
    assert run(aws, "--actor", RUNTIME, "--apply")[0] == 1
    assert (f"CHANNEL#{RUNTIME}", "PROFILE") not in aws.identity.items
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert ours(aws) == []


def test_every_version_and_delete_marker_goes_in_batches_of_1000():
    aws = world()
    for n in range(1700):
        aws.s3.add(f"{NS}/.openclaw/sessions/{n:04d}.jsonl", versions=1, marker=n < 600)
    aws.s3.add(f"_system/{NS}/USER.md", versions=250)
    total = len(aws.s3.keys(f"{NS}/")) + len(aws.s3.keys(f"_system/{NS}/"))
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert total == 2557 and aws.s3.batches == [1000, 1000, 557]
    assert aws.s3.keys(f"{NS}/") == [] and aws.s3.keys(f"_system/{NS}/") == []
    assert len(aws.s3.keys(f"{OTHER_NS}/")) == 5 and len(aws.s3.keys(f"_system/{OTHER_NS}/")) == 2
    listings = [call for call in aws.log if call[1] == "list_object_versions"]
    assert len(listings) > 4  # paginated, then listed again to check both prefixes are empty


def test_objects_written_after_the_first_pass_are_deleted_by_the_check():
    aws = world()
    aws.s3.after_delete = lambda s3: s3.add(f"{NS}/.openclaw/late-write.json")
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert "after 2 passes; both prefixes are now empty" in out
    assert aws.s3.keys(f"{NS}/") == []


def test_secrets_keep_a_recovery_window_unless_forced():
    aws = world()
    aws.secrets.add(f"openclaw/user/{NS}/OLD_KEY", pending=True)
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert [d.get("RecoveryWindowInDays") for d in aws.secrets.deletes] == [7, 7]
    assert not any(d.get("ForceDeleteWithoutRecovery") for d in aws.secrets.deletes)
    assert all(s.get("DeletedDate") for n, s in aws.secrets.secrets.items() if our_secret(n))
    assert not any(s.get("DeletedDate") for n, s in aws.secrets.secrets.items() if OTHER_NS in n)
    assert "(1 already scheduled)" in out and "can be restored until the recovery window ends" in out

    aws = world()
    aws.secrets.add(f"openclaw/user/{NS}/OLD_KEY", pending=True)
    code, out = run(aws, "--actor", RUNTIME, "--apply", "--force-delete-secrets")
    assert code == 0, out
    assert [d.get("ForceDeleteWithoutRecovery") for d in aws.secrets.deletes] == [True, True, True]
    assert not any("RecoveryWindowInDays" in d for d in aws.secrets.deletes)
    assert [n for n in aws.secrets.secrets if our_secret(n)] == []


def test_a_telegram_group_the_user_allowlisted_is_reported_not_deleted():
    aws = world()
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert aws.identity.items[(f"ALLOW#{GROUP}", "ALLOW")]["addedBy"] == RUNTIME
    assert f"{GROUP} stays allowlisted; its addedBy names this user" in out
    assert "kept 1 Telegram group allowlist record this user added" in out


def test_held_admissions_refuse_the_session_step():
    aws = world()
    session = aws.identity.items[(f"USER#{USER}", "SESSION")]
    session.update(activeAdmissions={"token-1"}, inFlight=1)
    code, out = run(aws, "--actor", RUNTIME)
    assert code == 3 and "--apply would stop here: the route holds 1 invocation admission" in out
    assert aws.log.mutating() == []

    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 3 and "REFUSED: step 3: the route holds 1 invocation admission" in out
    assert {step_of(call) for call in aws.log.mutating()} == {2}
    assert aws.agentcore.stopped == [] and session["routeStatus"] == "ACTIVE"
    assert len(aws.s3.keys(f"{NS}/")) == 5 and aws.identity.items[(f"USER#{USER}", "PROFILE")]

    del session["activeAdmissions"], session["inFlight"]
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert ours(aws) == []


def test_partition_items_alone_do_not_widen_the_deletion():
    aws = world()
    t = aws.identity
    # Stale or inconsistent items in this user's partition: another user's actor and schedule.
    t.put(PK=f"USER#{USER}", SK=f"CHANNEL#{OTHER}", channel="telegram", runtimeActorId=RUNTIME)
    t.put(PK=f"USER#{USER}", SK="CRON#aaaa0000", ebName=f"openclaw-{OTHER_NS}-aaaa0000")
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert all(theirs(aws))
    assert (f"ALLOW#{OTHER}", "ALLOW") in t.items and (f"CHANNEL#{OTHER}", "PROFILE") in t.items
    assert "1 CRON# record named a schedule outside this user's schedule names" in out
    assert "1 back-reference named an actor that no record outside the partition ties to this user" in out
    assert ours(aws) == []


@pytest.mark.parametrize("mismatch", [{"SESSION": OTHER}, {"PROFILE": OTHER}, {"SESSION": OTHER, "PROFILE": OTHER}])
def test_runtime_actor_records_that_disagree_are_refused_before_any_change(mismatch):
    aws = world()
    for sk, actor in mismatch.items():
        field = "actorId" if sk == "SESSION" else "enrollmentActorId"
        aws.identity.items[(f"USER#{USER}", sk)][field] = actor
    before = aws.state()
    code, out = run(aws, "--user-id", USER, "--apply")
    assert code == 3 and "REFUSED" in out, out
    assert aws.log.mutating() == [] and aws.state() == before


def test_a_stored_namespace_counts_only_with_its_ns_owner_record():
    stored = "ns_" + "a" * 26
    aws = world()
    aws.identity.items[(f"USER#{USER}", "SESSION")]["namespace"] = stored
    aws.identity.put(PK=f"NS#{stored}", SK="OWNER", userId=USER)
    aws.s3.add(f"{stored}/.openclaw/state.db", versions=2)
    aws.cognito.users.add(stored)
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 0, out
    assert aws.s3.keys(f"{stored}/") == [] and stored not in aws.cognito.users
    assert (f"NS#{stored}", "OWNER") not in aws.identity.items
    assert ours(aws) == []

    aws = world()
    aws.identity.items[(f"USER#{USER}", "SESSION")]["namespace"] = stored
    aws.identity.put(PK=f"NS#{stored}", SK="OWNER", userId=OTHER_USER)
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 3 and "not owned by this user" in out
    assert aws.log.mutating() == []


def test_resource_names_come_from_the_suffixed_stacks():
    aws = world(suffix="stg")
    code, out = run(aws, "--actor", RUNTIME, "--apply", suffix="stg")
    assert code == 0, out
    stacks = {call[2] for call in aws.log if call[1] == "describe_stacks"}
    assert stacks == {"OpenClawRouter-stg", "OpenClawTokenMonitoring-stg", "OpenClawAgentCore-stg",
                      "OpenClawSecurity-stg"}
    assert aws.scheduler.groups == {"openclaw-cron-stg"}
    assert ours(aws) == []

    aws = world()
    del aws.cfn.stacks["OpenClawRouter"]["IdentityTableName"]
    code, out = run(aws, "--actor", RUNTIME, "--apply")
    assert code == 1 and "[1] FAILED: the OpenClawRouter stack has no IdentityTableName output" in out
    assert aws.log.mutating() == []


def test_an_unknown_actor_has_nothing_to_delete():
    aws = world()
    code, out = run(aws, "--actor", "telegram:999999999", "--apply")
    assert code == 0 and "no identity records found; nothing to delete" in out
    assert aws.log.mutating() == []


@pytest.mark.parametrize("args", [
    ["--actor", "telegram 1"], ["--actor", "telegram:1", "--user-id", USER], ["--user-id", "user_xyz"],
    [], ["--actor", RUNTIME, "--env-suffix", "Bad_Suffix"],
])
def test_bad_arguments_exit_2(args):
    with pytest.raises(SystemExit) as raised:
        tool.main(["--region", "us-west-2", *args], aws=world(), out=io.StringIO())
    assert raised.value.code == 2


def test_schedule_names_follow_the_bridge_rule():
    assert tool.schedule_prefixes(NS) == [(f"openclaw-{NS}-", False)]
    long = "line_" + "U" + "0" * 50
    digest = hashlib.sha256(long.encode()).hexdigest()[:12]
    assert tool.schedule_prefixes(long) == [(f"openclaw-{long[:33]}-{digest}-", False),
                                            (f"openclaw-{long[:46]}-", True)]


def test_sample_dry_run_output(capsys):
    """Prints the dry-run report for the docs and the PR (pytest -s shows it)."""
    aws = world()
    code, out = run(aws, "--actor", RUNTIME)
    assert code == 0
    print(out)
