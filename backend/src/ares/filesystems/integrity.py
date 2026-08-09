"""Deterministic integrity helpers shared by planner and privileged broker."""

from __future__ import annotations

import hashlib
import json

from ares.filesystems.models import FilesystemRepairPlan


def repair_plan_fingerprint(plan: FilesystemRepairPlan) -> str:
    payload = plan.model_dump(mode="json", exclude={"fingerprint_sha256"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def repair_plan_integrity_valid(plan: FilesystemRepairPlan) -> bool:
    return repair_plan_fingerprint(plan) == plan.fingerprint_sha256
