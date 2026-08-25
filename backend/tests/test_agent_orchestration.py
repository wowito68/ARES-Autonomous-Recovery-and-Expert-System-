"""Operational agent, resource resolver and safe session contracts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ares.config import Environment, LogFormat, Settings
from ares.main import create_app
from ares.storage.models import (
    DiskSnapshot,
    FilesystemSnapshot,
    MountPointSnapshot,
    OperatingSystemSnapshot,
    PartitionSnapshot,
    StorageSummary,
    SystemStorageSnapshot,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}",
        runtime_state_dir=tmp_path / "run",
        capability_state_dir=tmp_path / "state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


def _write_inventory(settings: Settings) -> None:
    installed_root = settings.runtime_state_dir / "fixture-installed"
    (installed_root / "etc").mkdir(parents=True, exist_ok=True)
    (installed_root / "etc/os-release").write_text(
        'NAME="Fixture Linux"\nPRETTY_NAME="Fixture Linux 13"\nVERSION_ID="13"\nID=fixture\n',
        encoding="utf-8",
    )
    path = settings.runtime_state_dir / "hardware/public/inventory-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "probes": {
                    "storage": {
                        "status": "ok",
                        "data": {
                            "blockdevices": [
                                {
                                    "name": "sda",
                                    "type": "disk",
                                    "size": 512 * 1024**3,
                                    "model": "Fixture Internal SSD",
                                    "tran": "sata",
                                    "children": [
                                        {
                                            "name": "sda1",
                                            "type": "part",
                                            "size": 480 * 1024**3,
                                            "fstype": "ext4",
                                            "uuid": "11111111-2222-3333-4444-555555555555",
                                            "mountpoints": [str(installed_root)],
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


def _storage_snapshot(settings: Settings) -> SystemStorageSnapshot:
    installed_root = settings.runtime_state_dir / "fixture-installed"
    return SystemStorageSnapshot(
        session_id="test-session",
        evidence_sha256="sha256:" + "1" * 64,
        summary=StorageSummary(
            disk_count=1,
            partition_count=1,
            filesystem_count=1,
            mounted_filesystem_count=1,
            total_capacity_bytes=512 * 1024**3,
        ),
        disks=(
            DiskSnapshot(
                id="disk-fixture",
                name="sda",
                path="/dev/sda",
                size_bytes=512 * 1024**3,
                model="Fixture Internal SSD",
                transport="sata",
                partition_ids=("part-fixture-root",),
                smart_id="smart-fixture",
            ),
        ),
        partitions=(
            PartitionSnapshot(
                id="part-fixture-root",
                disk_id="disk-fixture",
                name="sda1",
                path="/dev/sda1",
                size_bytes=480 * 1024**3,
                filesystem_id="fs-fixture-root",
                mount_point_ids=("mnt-fixture-root",),
            ),
        ),
        filesystems=(
            FilesystemSnapshot(
                id="fs-fixture-root",
                device_path="/dev/sda1",
                filesystem_type="ext4",
                uuid="11111111-2222-3333-4444-555555555555",
            ),
        ),
        mounts=(
            MountPointSnapshot(
                id="mnt-fixture-root",
                source="/dev/sda1",
                path=str(installed_root),
                filesystem_type="ext4",
                options=("ro",),
            ),
        ),
        operating_systems=(
            OperatingSystemSnapshot(
                id="os-fixture-linux",
                name="Fixture Linux",
                version="13",
                os_id="fixture",
                source="/dev/sda1",
                mountpoint=str(installed_root),
                disk_id="disk-fixture",
            ),
        ),
        smart=(),
        warnings=(),
    )


async def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


async def test_resources_refresh_and_catalog_use_real_storage_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            refresh = await client.post("/api/v1/resources/refresh")
            catalog = await client.get("/api/v1/resources")

    assert refresh.status_code == 200
    assert catalog.status_code == 200
    payload = catalog.json()
    assert payload["snapshot_id"] == refresh.json()["snapshot_id"]
    assert any(item["kind"] == "disk" for item in payload["resources"])
    assert any("Disco interno" in item["human_name"] for item in payload["resources"])
    assert all(item["resource_id"] and item["stable_identity"] for item in payload["resources"])


async def test_agent_run_requires_authorization_then_executes_real_read_only_boot_diagnostic(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            created = await client.post(
                "/api/v1/agent/runs",
                json={"objective": "Revisa por qué no inicia mi computadora"},
            )
            run = created.json()
            rejected = await client.post(
                f"/api/v1/agent/runs/{run['id']}/authorize-read-only",
                json={},
            )
            authorized = await client.post(
                f"/api/v1/agent/runs/{run['id']}/authorize-read-only",
                json={"confirm": True, "understood": "AUTORIZO SOLO LECTURA"},
            )
            executed = await client.post(f"/api/v1/agent/runs/{run['id']}/execute")

    assert created.status_code == 201
    assert run["state"] == "READ_ONLY_AUTHORIZATION_REQUIRED"
    assert "boot.repair.grub" in " ".join(run["limitations"])
    assert [step["capability_id"] for step in run["steps"]] == [
        "storage.disk-analysis",
        "boot.diagnose",
    ]
    assert rejected.status_code == 409
    assert authorized.status_code == 200
    authorized_payload = authorized.json()
    assert authorized_payload["state"] == "PLAN_READY"
    assert authorized_payload["authorization"]["objective"] == run["objective"]
    assert authorized_payload["authorization"]["plan_fingerprint"].startswith("sha256:")
    assert authorized_payload["authorization"]["target_resource_id"]
    assert executed.status_code == 200
    finished = executed.json()
    assert finished["state"] in {"COMPLETED", "PARTIAL"}
    assert finished["authorization"]["consumed"] is True
    assert finished["steps"][0]["state"] == "COMPLETED"
    assert finished["steps"][0]["result"]["snapshot_id"]
    assert finished["steps"][1]["state"] == "COMPLETED"
    assert finished["steps"][1]["result"]["summary"].startswith("Diagnóstico de arranque")


async def test_boot_diagnose_endpoint_uses_existing_storage_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            refresh = await client.post("/api/v1/resources/refresh")
            response = await client.post(
                "/api/v1/boot/diagnose",
                json={"snapshot_id": refresh.json()["snapshot_id"]},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_id"] == refresh.json()["snapshot_id"]
    assert payload["repair_capability_available"] is False
    assert payload["findings"]
    assert "GRUB no fue verificado" in " ".join(payload["limitations"])


async def test_session_preflight_terminal_and_confirmed_action_do_not_reboot_in_test(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            preflight = await client.get("/api/v1/system/session/preflight/reboot")
            action = await client.post(
                "/api/v1/system/session/action",
                json={"operation": "reboot", "confirm": True, "understood": "ENTIENDO"},
            )
            contexts = await client.get("/api/v1/system/terminal/contexts")
            plan = await client.post(
                "/api/v1/system/terminal/plans",
                json={"context_kind": "ares_local"},
            )
            terminal = await client.post(
                f"/api/v1/system/terminal/plans/{plan.json()['plan']['id']}/authorize-open",
                json={"confirm": True},
            )

    assert preflight.status_code == 200
    assert preflight.json()["allowed"] is True
    assert action.status_code == 200
    assert action.json()["accepted"] is True
    assert action.json()["executed"] is False
    assert contexts.status_code == 200
    assert any(item["id"] == "ares_local" for item in contexts.json()["contexts"])
    assert plan.status_code == 200
    assert plan.json()["plan"]["requires_authorization"] is False
    assert terminal.status_code == 200
    assert terminal.json()["accepted"] is True
    request_id = terminal.json()["session"]["id"]
    assert request_id
    assert (settings.runtime_state_dir / "terminal" / f"{request_id}.json").is_file()


async def test_terminal_installed_read_only_requires_exact_authorization_and_redacts_paths(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_inventory(settings)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.storage_snapshot_store.put(_storage_snapshot(settings))
        async with await _client(app) as client:
            contexts = await client.get("/api/v1/system/terminal/contexts")
            plan = await client.post(
                "/api/v1/system/terminal/plans",
                json={"context_kind": "installed_system_read_only"},
            )
            rejected = await client.post(
                f"/api/v1/system/terminal/plans/{plan.json()['plan']['id']}/authorize-open",
                json={"confirm": True, "understood": "MAL"},
            )
            started = await client.post(
                f"/api/v1/system/terminal/plans/{plan.json()['plan']['id']}/authorize-open",
                json={"confirm": True, "understood": "AUTORIZO TERMINAL DE SOLO LECTURA"},
            )
            preflight = await client.get("/api/v1/system/session/preflight/reboot")
            closed = await client.post(
                f"/api/v1/system/terminal/sessions/{started.json()['session']['id']}/close",
                json={"confirm": True},
            )

    assert contexts.status_code == 200
    assert any(
        item["id"] == "installed_system_read_only" and item["available"]
        for item in contexts.json()["contexts"]
    )
    assert plan.status_code == 200
    public_plan = plan.json()["plan"]
    assert public_plan["requires_authorization"] is True
    assert public_plan["target"]["technical_path"] is None
    assert public_plan["target"]["technical_details"] == {}
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "TERMINAL_AUTHORIZATION_CONFIRMATION_REQUIRED"
    assert started.status_code == 200
    assert started.json()["session"]["status"] == "ACTIVE"
    assert preflight.status_code == 200
    assert preflight.json()["allowed"] is False
    assert any(item.startswith("terminal:") for item in preflight.json()["active_operations"])
    assert closed.status_code == 200
    assert closed.json()["session"]["status"] == "CLOSED"


async def test_terminal_plan_rejects_forbidden_command_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            response = await client.post(
                "/api/v1/system/terminal/plans",
                json={"context_kind": "ares_local", "command": "id"},
            )

    assert response.status_code == 422


async def test_session_preflight_blocks_active_runs_and_pending_terminals(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    active_dir = settings.capability_state_dir / "agent/runs"
    active_dir.mkdir(parents=True)
    (active_dir / "run-1.json").write_text(
        json.dumps({"id": "run-1", "state": "RUNNING_DIAGNOSTIC"}),
        encoding="utf-8",
    )
    terminal_dir = settings.runtime_state_dir / "terminal"
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "pending.json").write_text("{}", encoding="utf-8")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            preflight = await client.get("/api/v1/system/session/preflight/reboot")

    assert preflight.status_code == 200
    payload = preflight.json()
    assert payload["allowed"] is False
    assert payload["active_operations"] == ["runs:run-1:RUNNING_DIAGNOSTIC"]
    assert payload["prepared_terminals"] == ["pending.json"]
