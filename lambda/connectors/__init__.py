"""Curated connector SDK behind the trusted capability boundary (Task 10).

The connector plane is deliberately separate from the model-facing capability
catalog: a curated, release-owned registry of reviewed manifests, a synthetic
local MCP adapter that sits ENTIRELY behind the trusted boundary, and a browser
authority boundary that is disabled by default. Nothing dynamic is ever exposed
to the runtime; no real connector or authenticated browser is enabled here.
"""

from __future__ import annotations

try:
    from .manifest import (
        CURATED_CONNECTOR_IDS,
        build_curated_registry,
        compile_connector_manifest,
        manifest_digest,
    )
    from .mcp import (
        ConnectorCallTimeout,
        ConnectorOutputTooLarge,
        ConnectorRequestContext,
        InMemoryPreparedStore,
        ManifestDrift,
        SyntheticMcpConnectorAdapter,
    )
    from .synthetic import SyntheticMcpServer
except ImportError:  # pragma: no cover - bare-module load path (connector_*)
    from manifest import (  # type: ignore[no-redef]
        CURATED_CONNECTOR_IDS,
        build_curated_registry,
        compile_connector_manifest,
        manifest_digest,
    )
    from mcp import (  # type: ignore[no-redef]
        ConnectorCallTimeout,
        ConnectorOutputTooLarge,
        ConnectorRequestContext,
        InMemoryPreparedStore,
        ManifestDrift,
        SyntheticMcpConnectorAdapter,
    )
    from synthetic import SyntheticMcpServer  # type: ignore[no-redef]

# No real connector is enabled by default; the curated registry is the only
# reviewed surface and it is never wired into the production composition.
CONNECTORS_ENABLED_BY_DEFAULT = False

__all__ = [
    "CONNECTORS_ENABLED_BY_DEFAULT",
    "CURATED_CONNECTOR_IDS",
    "ConnectorCallTimeout",
    "ConnectorOutputTooLarge",
    "ConnectorRequestContext",
    "InMemoryPreparedStore",
    "ManifestDrift",
    "SyntheticMcpConnectorAdapter",
    "SyntheticMcpServer",
    "build_curated_registry",
    "compile_connector_manifest",
    "manifest_digest",
]
