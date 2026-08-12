"""Canonical fingerprints for storage identities, layouts and operation plans."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from ares.storage_operations.models import (
    PartitionTable,
    StorageDeviceIdentity,
    StorageLayout,
    StorageOperationPlan,
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def token_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def device_identity_fingerprint(identity: StorageDeviceIdentity | dict[str, Any]) -> str:
    payload = _payload(identity)
    payload.pop("fingerprint_sha256", None)
    return canonical_sha256(payload)


def partition_table_fingerprint(table: PartitionTable | dict[str, Any]) -> str:
    payload = _payload(table)
    payload.pop("fingerprint_sha256", None)
    partitions = payload.get("partitions")
    if isinstance(partitions, list):
        payload["partitions"] = [
            {
                key: item.get(key)
                for key in (
                    "number",
                    "start_sector",
                    "end_sector",
                    "size_sectors",
                    "size_bytes",
                    "type_code",
                    "partuuid",
                    "name",
                    "bootable",
                    "attrs",
                )
            }
            for item in partitions
            if isinstance(item, dict)
        ]
    return canonical_sha256(payload)


def layout_fingerprint(layout: StorageLayout | dict[str, Any]) -> str:
    payload = _payload(layout)
    payload.pop("id", None)
    payload.pop("observed_at", None)
    payload.pop("evidence_sha256", None)
    return canonical_sha256(payload)


def storage_plan_fingerprint(plan: StorageOperationPlan | dict[str, Any]) -> str:
    payload = _payload(plan)
    payload.pop("fingerprint_sha256", None)
    return canonical_sha256(payload)


def storage_plan_integrity_valid(plan: StorageOperationPlan) -> bool:
    return storage_plan_fingerprint(plan) == plan.fingerprint_sha256


def _payload(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def partition_geometry_signature(table: PartitionTable) -> tuple[object, ...]:
    return (
        table.type.value,
        table.guid,
        table.sector_size,
        tuple(
            (
                item.number,
                item.start_sector,
                item.size_sectors,
                (item.type_code or "").casefold(),
                (item.partuuid or "").casefold(),
                item.name,
                item.bootable,
            )
            for item in sorted(table.partitions, key=lambda item: item.number)
        ),
    )
