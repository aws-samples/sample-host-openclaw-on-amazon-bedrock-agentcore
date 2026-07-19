"""Trusted Browser Gateway authority boundary, disabled by default (Task 10).

The conversational runtime never owns a browser. Curated browsing is provided
here, OUTSIDE AgentCore, by a trusted gateway that is disabled unless an
explicit trusted flag, a profile ref, and an exact target allowlist are all
supplied. No submit/upload/send/delete acts directly: each returns an
ActionProposalV1 that must be dispatched through the Task 3 kernel.
"""

from __future__ import annotations

try:
    from .gateway import (
        BROWSER_ENABLED_BY_DEFAULT,
        BrowserDisabled,
        BrowserGateway,
        BrowserTargetDenied,
        FakeBrowserSession,
        TrustedProfileVault,
    )
except ImportError:  # pragma: no cover - bare-module load path (browser_gateway)
    from gateway import (  # type: ignore[no-redef]
        BROWSER_ENABLED_BY_DEFAULT,
        BrowserDisabled,
        BrowserGateway,
        BrowserTargetDenied,
        FakeBrowserSession,
        TrustedProfileVault,
    )

__all__ = [
    "BROWSER_ENABLED_BY_DEFAULT",
    "BrowserDisabled",
    "BrowserGateway",
    "BrowserTargetDenied",
    "FakeBrowserSession",
    "TrustedProfileVault",
]
