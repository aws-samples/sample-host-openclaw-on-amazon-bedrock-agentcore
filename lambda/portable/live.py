"""Strong-read, authority-free projections of one active portable generation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
from typing import Mapping

from .manifest import (
    FORMAT,
    ImportUncertain,
    RECORD_CATEGORIES,
    default_landing,
    safe_path,
    user_id,
)
from .records import validate_bundle_records


@dataclass(frozen=True, slots=True)
class PortableLiveSnapshot:
    generation: int
    bundle_hash: str | None
    records: dict[str, list[dict]]
    workspace: dict[str, bytes]


class PortableLiveProjection:
    """List/history projection; intentionally exposes no authority methods."""

    def __init__(self, store) -> None:
        if not callable(getattr(store, "load_live", None)):
            raise TypeError("portable live store is invalid")
        self._store = store

    @staticmethod
    def _empty() -> PortableLiveSnapshot:
        return PortableLiveSnapshot(
            generation=0,
            bundle_hash=None,
            records={category: [] for category in RECORD_CATEGORIES},
            workspace={},
        )

    def snapshot_for_user(self, target_user_id: str) -> PortableLiveSnapshot:
        target = user_id(target_user_id)
        live = self._store.load_live(target)
        if live is None:
            return self._empty()
        try:
            if (
                not isinstance(live, Mapping)
                or set(live) != {"userId", "generation", "bundleHash", "staged"}
                or live.get("userId") != target
                or isinstance(live.get("generation"), bool)
                or not isinstance(live.get("generation"), int)
                or live["generation"] < 1
                or not isinstance(live.get("bundleHash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", live["bundleHash"]) is None
            ):
                raise ValueError("outer live record")
            staged = live.get("staged")
            if (
                not isinstance(staged, Mapping)
                or set(staged) != {"format", "records", "workspace", "landing"}
                or staged.get("format") != FORMAT
                or staged.get("landing") != default_landing()
                or not isinstance(staged.get("workspace"), Mapping)
            ):
                raise ValueError("staged live record")
            records = validate_bundle_records(staged.get("records"))
            if any(
                row.get("userId") != target
                for category in ("schedules", "installed_packs")
                for row in records[category]
            ):
                raise ValueError("cross-user live record")
            workspace: dict[str, bytes] = {}
            for raw_path, descriptor in staged["workspace"].items():
                path = safe_path(raw_path)
                if (
                    not isinstance(descriptor, Mapping)
                    or set(descriptor) != {"encoding", "data", "sha256"}
                    or descriptor.get("encoding") != "base64"
                    or not isinstance(descriptor.get("data"), str)
                    or not isinstance(descriptor.get("sha256"), str)
                ):
                    raise ValueError("workspace descriptor")
                payload = base64.b64decode(descriptor["data"], validate=True)
                if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
                    raise ValueError("workspace hash")
                if path in workspace:
                    raise ValueError("workspace alias")
                workspace[path] = payload
        except Exception as error:
            if isinstance(error, ImportUncertain):
                raise
            raise ImportUncertain("portable live state is invalid") from error
        return PortableLiveSnapshot(
            generation=live["generation"],
            bundle_hash=live["bundleHash"],
            records=records,
            workspace=workspace,
        )

    def records_for_user(self, target_user_id: str) -> dict[str, list[dict]]:
        return self.snapshot_for_user(target_user_id).records

    def workspace_files(self, target_user_id: str) -> dict[str, bytes]:
        return self.snapshot_for_user(target_user_id).workspace

    def memory_records(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["memory"]

    def disabled_schedules(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["schedules"]

    def installed_pack_metadata(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["installed_packs"]

    def disconnected_connectors(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["connectors"]

    def compute_receipt_history(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["compute_receipts"]

    def effect_receipt_history(self, target_user_id: str) -> list[dict]:
        return self.records_for_user(target_user_id)["receipts"]


__all__ = ["PortableLiveProjection", "PortableLiveSnapshot"]
