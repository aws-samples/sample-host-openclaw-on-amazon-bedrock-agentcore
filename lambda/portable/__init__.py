"""Portable user state v2: content-addressed export and staged import.

The portable bundle is a byte-reproducible ZIP whose manifest binds every
object to a content address.  Import is a dry-run plan followed by an
activation that is bound to the exact complete-bundle hash and lands through a
single atomic compare-and-swap.  Credentials, sessions, grants, approvals,
runtime internals, pending effects, and deletion tombstones are excluded on
export and rejected on import; imported history can never replay a past effect.
"""

from __future__ import annotations

from capabilities.contracts import (
    ImportPlanV1,
    ImportReceiptV1,
    PortableStateManifestV2,
)

from .manifest import (
    EXCLUDE_CATEGORIES,
    FORMAT,
    INCLUDE_CATEGORIES,
    BundleIntegrityError,
    ImportRejected,
    ImportUncertain,
    PortableError,
    canonical_json,
    complete_bundle_hash,
    object_sha256,
    safe_path,
)
from .exporter import ExportBundleV2, PortableExporter
from .importer import PortableImporter, PreparedActivationV1
from .live import PortableLiveProjection, PortableLiveSnapshot

__all__ = [
    "EXCLUDE_CATEGORIES",
    "FORMAT",
    "INCLUDE_CATEGORIES",
    "BundleIntegrityError",
    "ImportRejected",
    "ImportUncertain",
    "PortableError",
    "canonical_json",
    "complete_bundle_hash",
    "object_sha256",
    "safe_path",
    "ExportBundleV2",
    "PortableExporter",
    "ImportPlanV1",
    "ImportReceiptV1",
    "PortableStateManifestV2",
    "PreparedActivationV1",
    "PortableImporter",
    "PortableLiveProjection",
    "PortableLiveSnapshot",
]
