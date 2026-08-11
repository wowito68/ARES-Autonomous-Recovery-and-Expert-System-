"""Canonical integrity helpers for Boot Recovery plans and checkpoint artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ares.boot.models import BootCheckpointArtifact, BootRepairPlan


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def boot_plan_fingerprint(plan: BootRepairPlan) -> str:
    checkpoint = plan.protection_checkpoint
    payload = {
        "repair_id": plan.repair_id,
        "session_id": plan.session_id,
        "diagnostic_id": plan.diagnostic_id,
        "target_os": plan.target_os.model_dump(mode="json"),
        "target_disk": plan.target_disk.model_dump(mode="json"),
        "target_esp": plan.target_esp.model_dump(mode="json") if plan.target_esp else None,
        "bootloader": plan.bootloader.model_dump(mode="json"),
        "current_configuration": plan.current_configuration.model_dump(mode="json"),
        "expected_configuration": plan.expected_configuration.model_dump(mode="json"),
        "issues": [item.model_dump(mode="json") for item in plan.issues],
        "operations": [item.model_dump(mode="json") for item in plan.operations],
        "dependencies": [item.model_dump(mode="json") for item in plan.dependencies],
        "boot_impact": plan.boot_impact.model_dump(mode="json"),
        "risk": plan.risk,
        "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
        "authorization_required": plan.authorization_required,
        "verification_strategy": plan.verification_strategy.model_dump(mode="json"),
        "executable": plan.executable,
        "limitations": plan.limitations,
    }
    return canonical_sha256(payload)


def boot_plan_integrity_valid(plan: BootRepairPlan) -> bool:
    return plan.fingerprint_sha256 == boot_plan_fingerprint(plan)


def boot_checkpoint_artifact_sha256(artifact: BootCheckpointArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"artifact_sha256", "created_at"})
    return canonical_sha256(payload)


def boot_checkpoint_artifact_valid(artifact: BootCheckpointArtifact) -> bool:
    return artifact.artifact_sha256 == boot_checkpoint_artifact_sha256(artifact)
