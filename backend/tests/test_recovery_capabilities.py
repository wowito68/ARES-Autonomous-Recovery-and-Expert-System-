"""Fail-closed integration coverage for the privileged recovery catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ares.config import Environment, LogFormat, Settings
from ares.main import create_app
from ares.recovery import RecoveryExecutionRequest

RECOVERY_RISKS = {
    "boot.repair-grub": "critical",
    "boot.repair-efi-entry": "high",
    "boot.rebuild-initramfs": "high",
    "boot.repair-fstab": "high",
    "kernel.rollback": "high",
    "kernel.reinstall": "high",
    "packages.rollback": "high",
    "services.restore-configuration": "high",
    "network.restore-configuration": "high",
    "files.recover": "high",
    "system.rollback-checkpoint": "high",
    "user.account-recovery": "critical",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}",
        runtime_state_dir=tmp_path / "run",
        capability_state_dir=tmp_path / "state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


def _write_inventory(settings: Settings) -> None:
    installed = settings.runtime_state_dir / "installed-debian"
    (installed / "etc").mkdir(parents=True)
    (installed / "etc/os-release").write_text(
        'ID=debian\nPRETTY_NAME="Debian GNU/Linux 13"\n', encoding="utf-8"
    )
    inventory = settings.runtime_state_dir / "hardware/public/inventory-v1.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "probes": {
                    "storage": {
                        "status": "ok",
                        "data": {
                            "blockdevices": [
                                {
                                    "name": "vda",
                                    "type": "disk",
                                    "size": 64 * 1024**3,
                                    "children": [
                                        {
                                            "name": "vda1",
                                            "type": "part",
                                            "size": 63 * 1024**3,
                                            "fstype": "ext4",
                                            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                            "mountpoints": [str(installed)],
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )


async def _client(settings: Settings) -> tuple[FastAPI, AsyncClient]:
    app = create_app(settings)
    client = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )
    return app, client


async def test_recovery_catalog_exposes_all_contracts_and_truthful_availability(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app, client = await _client(settings)
    async with app.router.lifespan_context(app), client:
        response = await client.get("/api/v1/capabilities")

    assert response.status_code == 200
    catalog = {item["id"]: item for item in response.json()["capabilities"]}
    assert RECOVERY_RISKS.keys() <= catalog.keys()
    for capability_id, expected_risk in RECOVERY_RISKS.items():
        item = catalog[capability_id]
        assert item["plugin_id"] == "ares.recovery-core"
        assert item["mode"] == "mutating"
        assert item["operation_class"] == "recover"
        assert item["risk_level"] == expected_risk
        assert item["enabled"] is False
        assert item["disabled_reason"].startswith("Bloqueada:")
        assert item["requires_authorization"] is True
        assert item["requires_protection_checkpoint"] is True
        assert item["supports_dry_run"] is True
        assert item["supports_verification"] is True
        assert item["input_schema"]["additionalProperties"] is False
        assert "command" not in item["input_schema"]["properties"]
    assert "physical_presence_challenge_id" in catalog["boot.repair-grub"]["input_schema"][
        "required"
    ]
    assert "physical_presence_challenge_id" in catalog["user.account-recovery"][
        "input_schema"
    ]["required"]


async def test_disabled_recovery_fails_closed_before_any_workflow(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app, client = await _client(settings)
    async with app.router.lifespan_context(app), client:
        response = await client.post(
            "/api/v1/capabilities/boot.repair-grub/executions",
            json={},
        )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "CAPABILITY_DISABLED"
    assert "broker boot.*" in response.json()["detail"]


async def test_reasoner_does_not_present_disabled_recovery_as_ready(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app, client = await _client(settings)
    async with app.router.lifespan_context(app), client:
        response = await client.post(
            "/api/v1/planner/plan",
            json={"goal": "reparar GRUB y reinstalar GRUB"},
        )

    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "stopped"
    assert plan["hypotheses"][0]["capability_id"] == "boot.repair-grub"
    assert plan["steps"] == []
    assert "Bloqueada:" in plan["stop_reason"]


async def test_agent_separates_read_only_authorization_from_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app, client = await _client(settings)
    async with app.router.lifespan_context(app), client:
        refreshed = await client.post("/api/v1/resources/refresh")
        assert refreshed.status_code == 200
        resources = (await client.get("/api/v1/resources")).json()["resources"]
        target = next(item for item in resources if item["kind"] == "operating_system")
        created = await client.post(
            "/api/v1/agent/runs",
            json={"objective": "reparar GRUB", "resource_id": target["resource_id"]},
        )
        assert created.status_code == 201
        run = created.json()
        mutation = next(
            item for item in run["steps"] if item["capability_id"] == "boot.repair-grub"
        )
        assert mutation["state"] == "BLOCKED"
        assert mutation["risk"] == "critical"
        assert mutation["requires_protection"] is True
        assert mutation["error_code"] == "RECOVERY_PROVIDER_UNAVAILABLE"

        authorized = await client.post(
            f"/api/v1/agent/runs/{run['id']}/authorize-read-only",
            json={"confirm": True, "understood": "AUTORIZO SOLO LECTURA"},
        )

    assert authorized.status_code == 200
    body = authorized.json()
    assert "boot.repair-grub" not in body["authorization"]["capability_ids"]
    mutation = next(item for item in body["steps"] if item["capability_id"] == "boot.repair-grub")
    assert mutation["state"] == "BLOCKED"


def test_recovery_request_rejects_shell_paths_and_unbound_fields() -> None:
    payload = {
        "session_id": "session-1234",
        "target_resource_id": "resource:target-1234",
        "target_resource_fingerprint_sha256": "a" * 64,
        "plan_id": "plan:12345678",
        "plan_fingerprint_sha256": "b" * 64,
        "protection_checkpoint_id": "checkpoint:12345678",
        "authorization_id": "authorization:12345678",
        "command": "grub-install /dev/sda",
        "argv": ["sh", "-c", "id"],
        "path": "/mnt/target",
    }

    with pytest.raises(ValidationError) as exc_info:
        RecoveryExecutionRequest.model_validate(payload)

    rejected = {error["loc"][0] for error in exc_info.value.errors()}
    assert {"command", "argv", "path"} <= rejected
