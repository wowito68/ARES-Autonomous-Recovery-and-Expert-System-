"""Deterministic integrity fingerprints for System Recovery plans and operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ares.recovery.models import RecoveryOperation, SystemRecoveryPlan


def recovery_plan_fingerprint(plan: SystemRecoveryPlan) -> str:
    payload = plan.model_dump(mode="json")
    payload.pop("fingerprint_sha256", None)
    encoded = json.dumps(_stable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def recovery_plan_integrity_valid(plan: SystemRecoveryPlan) -> bool:
    return plan.fingerprint_sha256 == recovery_plan_fingerprint(plan)


def recovery_operation_fingerprint(operation: RecoveryOperation) -> str:
    encoded = json.dumps(
        _stable(operation.model_dump(mode="json")),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value
