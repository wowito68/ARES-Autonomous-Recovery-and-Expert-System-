from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ares.audit import MemoryAuditLedger
from ares.boot.models import BootRepairPlanInput
from ares.cli import build_parser
from ares.reasoning.models import ReasoningRequest
from ares.runtime.consent import ConsentAuthority
from tests.test_boot_engine import _diagnostic, _engine
from tests.test_boot_tools import _root


def test_boot_is_registered_across_composition_api_cli_and_reasoning(app: FastAPI) -> None:
    assert app.state.boot_recovery_service is not None
    assert app.state.boot_engine is not None
    assert app.state.boot_recovery_store is not None
    catalog = {item.id for item in app.state.capability_manager.catalog()}
    assert "boot.diagnose" in catalog
    assert "boot.repair.grub" in catalog

    paths = app.openapi()["paths"]
    assert "/api/v1/boot/diagnose" in paths
    assert "/api/v1/boot/repair/plan" in paths
    assert "/api/v1/boot/repair" in paths
    assert "/api/v1/boot/repairs/{repair_id}" in paths
    assert "/api/v1/boot/repairs/{repair_id}/verification" in paths
    assert "/api/v1/boot/repairs/{repair_id}/cancel" in paths

    parser = build_parser()
    assert parser.parse_args(["boot", "diagnose", "--root-path", "/mnt/linux"]).boot_command == "diagnose"
    assert parser.parse_args(["boot", "plan", "diagnostic123"]).boot_command == "plan"
    assert parser.parse_args(["boot", "repair", "plan12345"]).boot_command == "repair"
    assert parser.parse_args(["boot", "status", "repair123"]).boot_command == "status"
    assert parser.parse_args(["boot", "verify", "repair123"]).boot_command == "verify"
    assert parser.parse_args(["boot", "cancel", "repair123"]).boot_command == "cancel"
    assert parser.parse_args(["boot", "reconcile", "repair123"]).boot_command == "reconcile"

    assessment = app.state.reasoning_engine.assess(
        ReasoningRequest(goal="Mi Linux no arranca y GRUB parece dañado")
    )
    assert assessment.selected_capability_id == "boot.diagnose"


async def test_boot_consent_challenge_is_exact_and_independent(tmp_path: Path) -> None:
    root = _root(tmp_path, grub=False, initramfs=False)
    engine, store, _ = _engine(tmp_path)
    diagnostic = _diagnostic(root)
    await store.put_diagnostic(diagnostic)
    plan = await engine.plan(
        BootRepairPlanInput(diagnostic_id=diagnostic.id),
        session_id="boot-consent-session",
    )
    protected = await engine.protect(plan.id, session_id="boot-consent-session")
    assert protected.protection_checkpoint is not None

    authority = ConsentAuthority(MemoryAuditLedger())
    challenge = await authority.dispatch(
        {"action": "boot.create", "plan": protected.model_dump(mode="json")},
        peer_uid=0,
    )
    assert challenge["kind"] == "boot_repair"
    assert challenge["capability_id"] == "boot.repair.grub"
    assert challenge["risk"] == "high"
    assert challenge["checkpoint_id"] == protected.protection_checkpoint.id
    assert challenge["target_fingerprint"] == protected.target_disk.fingerprint_sha256
    assert challenge["confirmation_phrase"].endswith(protected.fingerprint_sha256[:12])
    assert "modifies boot state" in challenge["confirmation_phrase"]
    assert challenge["offline_verification_reboot_proof"] is False

    denied = await authority.dispatch(
        {"action": "deny", "challenge_id": challenge["challenge_id"]},
        peer_uid=1000,
    )
    assert denied["decision"] == "denied"
