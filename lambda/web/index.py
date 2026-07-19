"""Dependency-injected HTTP boundary for the trusted consumer control surface."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
from typing import Callable, Mapping
from urllib.parse import unquote_to_bytes

from workflows.gmail.repository import READONLY_PROVIDER

from portable.manifest import (
    BundleIntegrityError,
    ImportRejected,
    ImportUncertain,
    PortableError,
)

from .auth import AuthenticationError, ConnectTicketError
from .overview import ConnectionDisconnectPending
from .retention import DeletionPending, ExportBoundaryError


LOG = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_BYTES = 16 * 1024
MAX_IMPORT_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_IMPORT_BODY_BYTES = 4 * ((MAX_IMPORT_BUNDLE_BYTES + 2) // 3) + 4_096
_ACTION_ROUTE = re.compile(
    r"/api/actions/(?P<action>[A-Za-z0-9_-]{8,128})/(?P<verb>approve|reject)"
)
_GMAIL_DRAFT_ROUTE = re.compile(
    r"/api/gmail/drafts/(?P<action>[A-Za-z0-9_-]{8,128})"
)
_SCAN_FEEDBACK_ROUTE = re.compile(
    r"/api/scans/(?P<scan>scan_[0-9]{20}_[A-Za-z0-9_-]{32})/feedback"
)
_SCHEDULE_PROPOSAL_ROUTE = re.compile(
    r"/api/schedule-proposals/(?P<proposal>[A-Za-z0-9][A-Za-z0-9_-]{7,127})"
    r"(?:/(?P<verb>approve|reject|reconcile))?"
)
_APPROVAL_PREVIEW = re.compile(r"/approve/(?P<token>[A-Za-z0-9_.-]{1,4096})")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ORIGIN_HEADER = "x-personal-operator-origin-verify"
_ORIGIN_SECRET_CACHE_SECONDS = 300
_origin_secret_cache: tuple[str, float] | None = None


def _scheduled_retention_event(event: object) -> bool:
    return event == {
        "detail-type": "ScheduledRetentionSweep",
        "source": "personal-operator.retention",
        "version": 1,
    }


def _origin_verification_secret() -> str:
    """Resolve the domain-separated CloudFront origin proof with a short cache."""

    global _origin_secret_cache
    now = time.monotonic()
    if _origin_secret_cache is not None and now < _origin_secret_cache[1]:
        return _origin_secret_cache[0]
    secret_id = os.environ.get("ORIGIN_VERIFICATION_SECRET_ID", "")
    region = os.environ.get("AWS_REGION_LOCK", "")
    if not secret_id or region != "eu-west-1":
        raise RuntimeError("origin verification is not configured")
    import boto3
    from botocore.config import Config

    try:
        response = boto3.client(
            "secretsmanager",
            region_name=region,
            config=Config(
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 0},
            ),
        ).get_secret_value(SecretId=secret_id)
        secret = response.get("SecretString")
    except Exception:
        raise RuntimeError("origin verification secret is unavailable") from None
    if not isinstance(secret, str) or not 32 <= len(secret) <= 512:
        raise RuntimeError("origin verification secret is invalid")
    _origin_secret_cache = (secret, now + _ORIGIN_SECRET_CACHE_SECONDS)
    return secret


def _trusted_cloudfront_request(event: object) -> bool:
    """Authenticate the non-viewer-controlled header before route handling."""

    if not isinstance(event, Mapping):
        return False
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    if not isinstance(http, Mapping):
        return False
    supplied = _headers(event).get(_ORIGIN_HEADER)
    expected = _origin_verification_secret()
    return (
        isinstance(supplied, str)
        and bool(supplied)
        and hmac.compare_digest(supplied, expected)
    )


def _origin_gate_response(status: int) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"error": "request origin rejected"}, separators=(",", ":")),
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("request body contains duplicate fields")
        result[key] = value
    return result


def _json_body(event: Mapping, *, max_bytes: int = MAX_BODY_BYTES) -> Mapping:
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode()
        except Exception as error:
            raise ValueError("request body is invalid") from error
    if not isinstance(body, str) or len(body.encode()) > max_bytes:
        raise ValueError("request body is invalid")
    try:
        parsed = json.loads(body, object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError) as error:
        raise ValueError("request body is invalid") from error
    if not isinstance(parsed, Mapping):
        raise ValueError("request body must be an object")
    return parsed


def _portable_bundle(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("import bundle is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as error:
        raise ValueError("import bundle is invalid") from error
    if not decoded or len(decoded) > MAX_IMPORT_BUNDLE_BYTES:
        raise ValueError("import bundle is invalid")
    return decoded


def _oauth_callback_query(event: Mapping) -> dict[str, str]:
    raw = event.get("rawQueryString")
    if not isinstance(raw, str):
        raise ValueError("OAuth callback fields are invalid")
    try:
        encoded = raw.encode("ascii", "strict")
    except UnicodeEncodeError as error:
        raise ValueError("OAuth callback fields are invalid") from error
    if not encoded or len(encoded) > MAX_QUERY_BYTES:
        raise ValueError("OAuth callback fields are invalid")

    result: dict[str, str] = {}
    for field in raw.split("&"):
        if not field or "=" not in field or _INVALID_PERCENT_ESCAPE.search(field):
            raise ValueError("OAuth callback fields are invalid")
        name, value = field.split("=", 1)
        try:
            decoded_name = unquote_to_bytes(name.replace("+", " ")).decode(
                "ascii", "strict"
            )
            decoded_value = unquote_to_bytes(value.replace("+", " ")).decode(
                "ascii", "strict"
            )
        except UnicodeDecodeError as error:
            raise ValueError("OAuth callback fields are invalid") from error
        if decoded_name in result:
            raise ValueError("OAuth callback fields are invalid")
        result[decoded_name] = decoded_value

    if (
        set(result) != {"state", "code"}
        or not 1 <= len(result["state"]) <= 512
        or not 1 <= len(result["code"]) <= 4_096
        or "\x00" in result["state"]
        or "\x00" in result["code"]
    ):
        raise ValueError("OAuth callback fields are invalid")
    return result


def _headers(event: Mapping) -> dict[str, str]:
    raw = event.get("headers")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name).casefold(): value
        for name, value in raw.items()
        if isinstance(value, str)
    }


class WebApplication:
    def __init__(
        self,
        *,
        tickets,
        sessions,
        oauth,
        approvals,
        workspace,
        gmail_workspace,
        exporter,
        importer,
        deletion,
        retention,
        overview,
        connections,
        scans,
        schedule_control,
        web_origin: str,
        google_redirect_uri: str,
    ) -> None:
        if not web_origin.startswith("https://") or not google_redirect_uri.startswith(
            f"{web_origin}/"
        ):
            raise ValueError("web origin and redirect URI must be exact HTTPS URLs")
        self._tickets = tickets
        self._sessions = sessions
        self._oauth = oauth
        self._approvals = approvals
        self._workspace = workspace
        self._gmail_workspace = gmail_workspace
        self._exporter = exporter
        if any(
            not callable(getattr(importer, method, None))
            for method in ("build_plan", "prepare_activation", "activate")
        ):
            raise TypeError("portable importer is invalid")
        self._importer = importer
        self._deletion = deletion
        self._retention = retention
        if not callable(getattr(overview, "get", None)):
            raise TypeError("overview service is invalid")
        if not callable(getattr(connections, "disconnect", None)):
            raise TypeError("connection lifecycle is invalid")
        if not callable(getattr(scans, "feedback", None)):
            raise TypeError("scan measurement store is invalid")
        if any(
            not callable(getattr(schedule_control, method, None))
            for method in ("preview", "approve", "reject", "reconcile")
        ):
            raise TypeError("schedule control client is invalid")
        self._overview = overview
        self._connections = connections
        self._scans = scans
        self._schedule_control = schedule_control
        self._origin = web_origin.rstrip("/")
        self._redirect = google_redirect_uri

    @staticmethod
    def _response(status: int, body: object = None, *, headers=None) -> dict:
        response_headers = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            **(headers or {}),
        }
        if body is None:
            serialized = ""
        elif isinstance(body, str):
            serialized = body
        else:
            serialized = json.dumps(body, separators=(",", ":"), default=str)
            response_headers.setdefault("Content-Type", "application/json")
        return {"statusCode": status, "headers": response_headers, "body": serialized}

    def _identity(self, headers: Mapping[str, str], *, mutate: bool = False):
        return self._sessions.authenticate(
            cookie_header=headers.get("cookie"),
            csrf_token=headers.get("x-po-csrf"),
            require_csrf=mutate,
        )

    def handle(self, event: object) -> dict:
        if not isinstance(event, Mapping):
            return self._response(400, {"error": "invalid request"})
        scheduled = {
            "detail-type": "ScheduledRetentionSweep",
            "source": "personal-operator.retention",
            "version": 1,
        }
        if _scheduled_retention_event(event):
            result = self._retention.sweep()
            if not isinstance(result, Mapping):
                raise RuntimeError("retention sweep returned an invalid result")
            return dict(result)
        if "detail-type" in event or event.get("source") == scheduled["source"]:
            return self._response(400, {"error": "invalid scheduled event"})
        request_context = event.get("requestContext")
        http = request_context.get("http") if isinstance(request_context, Mapping) else None
        method = http.get("method") if isinstance(http, Mapping) else None
        path = http.get("path") if isinstance(http, Mapping) else event.get("rawPath")
        headers = _headers(event)
        supplied_origin = headers.get("origin")
        if supplied_origin is not None and supplied_origin != self._origin:
            return self._response(403, {"error": "origin rejected"})
        if method == "POST" and supplied_origin != self._origin:
            return self._response(403, {"error": "origin rejected"})
        allowed_methods = {"GET", "POST"}
        if method not in allowed_methods:
            return self._response(405, {"error": "method not allowed"})
        try:
            if method == "POST" and path == "/api/session/connect":
                body = _json_body(event)
                if set(body) != {"ticket"}:
                    raise ValueError("connect request fields are invalid")
                existing = None
                if headers.get("cookie") is not None:
                    try:
                        existing = self._identity(headers)
                    except AuthenticationError:
                        # A dead incumbent cookie must not strand a valid
                        # one-time Telegram ticket. Storage failures are not
                        # AuthenticationError and remain retryable failures.
                        existing = None
                redemption = self._tickets.consume(
                    body["ticket"],
                    expected_user_id=(
                        existing.user_id if existing is not None else None
                    ),
                )
                issued = self._sessions.issue(user_id=redemption.user_id)
                return self._response(
                    201,
                    {
                        "csrfToken": issued.csrf_token,
                        "expiresAt": issued.expires_at,
                        "returnPath": redemption.return_path,
                    },
                    headers={"Set-Cookie": issued.cookie},
                )

            if method == "GET" and path == "/oauth/google/start":
                identity = self._identity(headers)
                authorization = self._oauth.start(
                    user_id=identity.user_id,
                    redirect_uri=self._redirect,
                )
                return self._response(302, headers={"Location": authorization.url})

            if method == "GET" and path == "/oauth/google/callback":
                identity = self._identity(headers)
                query = _oauth_callback_query(event)
                self._oauth.complete(
                    user_id=identity.user_id,
                    state=query["state"],
                    code=query["code"],
                )
                return self._response(
                    302,
                    headers={"Location": f"{self._origin}/connections?google=connected"},
                )

            preview = _APPROVAL_PREVIEW.fullmatch(path or "")
            if method == "GET" and preview:
                identity = self._identity(headers)
                # Preview decodes and strongly reads only. It must never consume
                # a token or move an action out of APPROVAL_PENDING.
                result = self._approvals.preview(
                    token=preview.group("token"),
                    acting_user_id=identity.user_id,
                )
                return self._response(200, result)

            action = _ACTION_ROUTE.fullmatch(path or "")
            if method == "POST" and action:
                identity = self._identity(headers, mutate=True)
                body = _json_body(event)
                if action.group("verb") == "approve":
                    if set(body) != {"token", "revision", "args"}:
                        raise ValueError("approval request fields are invalid")
                    result = self._approvals.approve(
                        action_id=action.group("action"),
                        revision=body["revision"],
                        acting_user_id=identity.user_id,
                        token=body["token"],
                        args=body["args"],
                    )
                else:
                    if set(body) != {"revision"}:
                        raise ValueError("rejection request fields are invalid")
                    result = self._approvals.reject(
                        action_id=action.group("action"),
                        revision=body["revision"],
                        acting_user_id=identity.user_id,
                    )
                return self._response(200, result)

            schedule_proposal = _SCHEDULE_PROPOSAL_ROUTE.fullmatch(path or "")
            if schedule_proposal:
                proposal_ref = schedule_proposal.group("proposal")
                verb = schedule_proposal.group("verb")
                if method == "GET" and verb is None:
                    identity = self._identity(headers)
                    return self._response(
                        200,
                        self._schedule_control.preview(
                            user_id=identity.user_id,
                            proposal_ref=proposal_ref,
                        ),
                    )
                if method == "POST" and verb is not None:
                    identity = self._identity(headers, mutate=True)
                    body = _json_body(event)
                    if verb == "reconcile":
                        if body != {}:
                            raise ValueError(
                                "schedule reconciliation fields are invalid"
                            )
                        result = self._schedule_control.reconcile(
                            user_id=identity.user_id,
                            proposal_ref=proposal_ref,
                        )
                    else:
                        if set(body) != {"revision", "argsHash"}:
                            raise ValueError("schedule decision fields are invalid")
                        result = getattr(self._schedule_control, verb)(
                            user_id=identity.user_id,
                            proposal_ref=proposal_ref,
                            revision=body["revision"],
                            args_hash=body["argsHash"],
                        )
                    return self._response(200, result)

            if method == "GET" and path == "/api/workspace":
                identity = self._identity(headers)
                return self._response(200, self._workspace.get(identity.user_id))

            if method == "GET" and path == "/api/overview":
                identity = self._identity(headers)
                return self._response(200, self._overview.get(identity.user_id))

            if (
                method == "POST"
                and path
                == f"/api/connections/{READONLY_PROVIDER}/disconnect"
            ):
                identity = self._identity(headers, mutate=True)
                if _json_body(event) != {}:
                    raise ValueError("disconnect request fields are invalid")
                try:
                    status = self._connections.disconnect(identity.user_id)
                except ConnectionDisconnectPending:
                    # The bounded purge made progress but is not finished. Report
                    # the truthful pending state so the client keeps retrying;
                    # never claim DISCONNECTED while the fence is DISCONNECTING.
                    return self._response(
                        202,
                        {
                            "provider": READONLY_PROVIDER,
                            "status": "DISCONNECTING",
                            "remoteGrantRevoked": False,
                        },
                    )
                if status != "DISCONNECTED":
                    raise RuntimeError("connection disconnect returned invalid status")
                return self._response(
                    200,
                    {
                        "provider": READONLY_PROVIDER,
                        "status": status,
                        "remoteGrantRevoked": False,
                    },
                )

            if method == "POST" and path == "/api/session/logout":
                self._identity(headers, mutate=True)
                if _json_body(event) != {}:
                    raise ValueError("logout request fields are invalid")
                self._sessions.revoke(cookie_header=headers.get("cookie"))
                return self._response(
                    204,
                    headers={
                        "Set-Cookie": (
                            "__Host-po_session=; Path=/; Secure; HttpOnly; "
                            "SameSite=Lax; Max-Age=0"
                        )
                    },
                )

            scan_feedback = _SCAN_FEEDBACK_ROUTE.fullmatch(path or "")
            if method == "POST" and scan_feedback:
                identity = self._identity(headers, mutate=True)
                body = _json_body(event)
                if set(body) != {"response"} or body.get("response") not in {
                    "USEFUL",
                    "NOT_USEFUL",
                }:
                    raise ValueError("scan feedback is invalid")
                scan_id = scan_feedback.group("scan")
                result = self._scans.feedback(
                    identity.user_id,
                    scan_id,
                    response=body["response"],
                )
                if (
                    not isinstance(result, Mapping)
                    or result.get("scanId") != scan_id
                    or result.get("feedback") != body["response"]
                ):
                    raise RuntimeError("scan feedback result is invalid")
                return self._response(
                    200,
                    {"scanId": scan_id, "feedback": body["response"]},
                )

            if method == "GET" and path == "/api/gmail":
                identity = self._identity(headers)
                return self._response(
                    200, self._gmail_workspace.get(identity.user_id)
                )

            gmail_draft = _GMAIL_DRAFT_ROUTE.fullmatch(path or "")
            if method == "POST" and gmail_draft:
                identity = self._identity(headers, mutate=True)
                body = _json_body(event)
                if set(body) != {"revision", "subject", "body"}:
                    raise ValueError("draft edit request fields are invalid")
                result = self._gmail_workspace.edit_draft(
                    user_id=identity.user_id,
                    action_id=gmail_draft.group("action"),
                    revision=body["revision"],
                    subject=body["subject"],
                    body=body["body"],
                )
                return self._response(200, result)

            if method == "GET" and path == "/api/export":
                identity = self._identity(headers)
                archive = self._exporter.build_zip(identity.user_id)
                response = self._response(
                    200,
                    base64.b64encode(archive).decode(),
                    headers={
                        "Content-Type": "application/zip",
                        "Content-Disposition": 'attachment; filename="personal-operator-portable-v2.zip"',
                    },
                )
                response["isBase64Encoded"] = True
                return response

            if method == "POST" and path == "/api/delete":
                identity = self._identity(headers, mutate=True)
                body = _json_body(event)
                if body != {"confirm": "DELETE"}:
                    raise ValueError("exact deletion confirmation is required")
                return self._response(200, self._deletion.delete(identity.user_id))

            if method == "POST" and path == "/api/import/plan":
                identity = self._identity(headers, mutate=True)
                body = _json_body(event, max_bytes=MAX_IMPORT_BODY_BYTES)
                if set(body) != {"bundle"}:
                    raise ValueError("import plan request fields are invalid")
                bundle = _portable_bundle(body["bundle"])
                plan = self._importer.build_plan(
                    bundle,
                    target_user_id=identity.user_id,
                )
                return self._response(200, plan.to_mapping())

            if method == "POST" and path == "/api/import/activate":
                identity = self._identity(headers, mutate=True)
                body = _json_body(event, max_bytes=MAX_IMPORT_BODY_BYTES)
                if set(body) != {
                    "bundle",
                    "bundleHash",
                    "planId",
                    "baseGeneration",
                    "confirm",
                }:
                    raise ValueError("import activation request fields are invalid")
                if body.get("confirm") is not True:
                    raise ValueError("import activation must be explicitly confirmed")
                bundle_hash = body.get("bundleHash")
                plan_id = body.get("planId")
                base_generation = body.get("baseGeneration")
                if not all(
                    isinstance(value, str)
                    for value in (bundle_hash, plan_id, base_generation)
                ):
                    raise ValueError("import activation plan is invalid")
                bundle = _portable_bundle(body["bundle"])
                prepared = self._importer.prepare_activation(
                    bundle,
                    target_user_id=identity.user_id,
                    approved_bundle_hash=bundle_hash,
                    approved_plan_id=plan_id,
                    approved_base_generation=base_generation,
                )
                if (
                    prepared.bundle_hash != bundle_hash
                    or prepared.plan_id != plan_id
                    or prepared.base_generation != base_generation
                ):
                    raise ImportUncertain(
                        "portable activation preparation is inconsistent"
                    )
                receipt = self._importer.activate(
                    bundle,
                    approved_bundle_hash=bundle_hash,
                    approved_plan_id=plan_id,
                    approved_base_generation=base_generation,
                    target_user_id=identity.user_id,
                    activation_approval=prepared.activation_approval,
                    expected_generation=prepared.expected_generation,
                )
                return self._response(200, receipt.to_mapping())

            known_paths = {
                "/api/session/connect",
                "/oauth/google/start",
                "/oauth/google/callback",
                "/api/workspace",
                "/api/overview",
                "/api/gmail",
                "/api/export",
                "/api/delete",
                "/api/import/plan",
                "/api/import/activate",
                f"/api/connections/{READONLY_PROVIDER}/disconnect",
                "/api/session/logout",
            }
            if (
                path in known_paths
                or action
                or preview
                or gmail_draft
                or scan_feedback
                or schedule_proposal
            ):
                return self._response(405, {"error": "method not allowed"})
            return self._response(404, {"error": "not found"})
        except (AuthenticationError,) as error:
            return self._response(401, {"error": str(error)})
        except ImportUncertain:
            # Activation could not be confirmed. No partial state exists; the
            # client may retry once the staging backend recovers.
            return self._response(409, {"error": "operation could not be completed"})
        except (BundleIntegrityError, ImportRejected, PortableError) as error:
            return self._response(400, {"error": str(error)})
        except (ConnectTicketError, ValueError, TypeError, ExportBoundaryError) as error:
            return self._response(400, {"error": str(error)})
        except PermissionError as error:
            return self._response(403, {"error": str(error)})
        except DeletionPending:
            return self._response(202, {"status": "deletion_pending"})
        except RuntimeError as error:
            LOG.warning("Web control operation failed: error_type=%s", type(error).__name__)
            return self._response(409, {"error": "operation could not be completed"})


_application_factory: Callable[[], WebApplication] | None = None
_production_application: WebApplication | None = None


def configure_application_factory(factory: Callable[[], WebApplication]) -> None:
    global _application_factory
    if not callable(factory):
        raise TypeError("web application factory must be callable")
    _application_factory = factory


def _application() -> WebApplication:
    global _production_application
    if _application_factory is not None:
        application = _application_factory()
    else:
        if _production_application is None:
            from .composition import build_production_application

            _production_application = build_production_application()
        application = _production_application
    if not isinstance(application, WebApplication):
        raise TypeError("web application factory returned an invalid value")
    return application


def lambda_handler(event, context):
    del context
    if not _scheduled_retention_event(event):
        try:
            trusted = _trusted_cloudfront_request(event)
        except RuntimeError as error:
            LOG.warning(
                "CloudFront origin verification unavailable: error_type=%s",
                type(error).__name__,
            )
            return _origin_gate_response(503)
        if not trusted:
            return _origin_gate_response(403)
    return _application().handle(event)
