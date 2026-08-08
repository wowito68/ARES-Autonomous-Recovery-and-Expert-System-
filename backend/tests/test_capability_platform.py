"""Extensibility contracts completed after the first ARES v2 vertical slice."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from ares.actions import ActionContext
from ares.capabilities import (
    AuditPolicy,
    CapabilityCategory,
    CapabilityManager,
    CapabilityMetadata,
    OperationClass,
    OSCompatibility,
    PermissionRequirement,
    PluginDiscoveryError,
    PluginManifest,
    RiskLevel,
    RollbackPolicy,
    discover_plugins,
)
from ares.config import Environment, LogFormat, Settings
from ares.events import EventBus, MemoryEventSink
from ares.knowledge import KnowledgeGraph
from ares.main import create_app
from ares.workflows import (
    StageMode,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStage,
    WorkflowStep,
)
from ares.workflows.models import StepOutputs


def _settings(tmp_path: Path, runtime: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'platform.db'}",
        runtime_state_dir=runtime,
        capability_state_dir=tmp_path / "capability-state",
        storage_process_probes_enabled=False,
        log_level="CRITICAL",
        log_format=LogFormat.TEXT,
    )


def _write_inventory(runtime: Path) -> None:
    path = runtime / "hardware/public/inventory-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "generation": 1,
                "probes": {
                    "storage": {
                        "status": "ok",
                        "data": {"blockdevices": []},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


async def test_catalog_generates_input_docs_versions_and_command_free_plan(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    _write_inventory(runtime)
    application = create_app(_settings(tmp_path, runtime))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            detail = await client.get("/api/v1/capabilities/storage.disk-analysis")
            versions = await client.get("/api/v1/capabilities/storage.disk-analysis/versions")
            needs_evidence = await client.post(
                "/api/v1/planner/plan",
                json={"goal": "analizar capacidad del disco"},
            )
            ready = await client.post(
                "/api/v1/planner/plan",
                json={
                    "goal": "analizar capacidad del disco",
                    "evidence": [{"id": "hardware.block-devices", "confidence": 0.95}],
                },
            )

    assert detail.status_code == versions.status_code == 200
    capability = detail.json()
    assert capability["plugin_id"] == "ares.storage-core"
    assert capability["active"] is True
    assert capability["mode"] == "read_only"
    assert capability["input_schema"]["additionalProperties"] is False
    assert capability["input_schema"]["properties"]["scope"]["default"] == "all_detected"
    assert capability["output_schema"]["title"] == "StorageCapabilityResult"
    assert versions.json()["count"] == 1

    assert needs_evidence.json()["status"] == "needs_evidence"
    plan = ready.json()
    assert plan["status"] == "ready"
    assert [step["capability_id"] for step in plan["steps"]] == ["storage.disk-analysis"]
    serialized = json.dumps(plan).casefold()
    assert "action" not in serialized
    assert "command" not in serialized
    assert "tool" not in serialized


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int
    version: str


@dataclass
class _EchoAction:
    id: str = "test.echo"
    idempotent: bool = True

    async def run(self, inputs: dict[str, Any], context: ActionContext) -> dict[str, Any]:
        del context
        return dict(inputs)

    async def compensate(
        self,
        output: dict[str, Any],
        context: ActionContext,
    ) -> None:
        del output, context


class _VersionedCapability:
    input_model = _Input
    output_model = _Output

    def __init__(self, version: str) -> None:
        compatibility = OSCompatibility(families=("debian",), architectures=("amd64",))
        self.metadata = CapabilityMetadata(
            id="test.versioned-capability",
            version=version,
            name="Versioned capability",
            description="A versioned capability used to validate registry selection.",
            objective="Prove that compatible capability versions can coexist safely.",
            category=CapabilityCategory.HARDWARE,
            operation=OperationClass.OBSERVE,
            os_compatibility=compatibility,
            risk=RiskLevel.LOW,
            estimated_duration_seconds=1,
            permissions=(
                PermissionRequirement(id="test.observe", reason="Read deterministic test data."),
            ),
            internal_actions=("test.echo",),
            postchecks=(),
            rollback=RollbackPolicy(supported=False, strategy="No changes are performed."),
            emitted_events=("capability.completed",),
            metrics=("test.duration_ms",),
            audit=AuditPolicy(
                record_inputs=True,
                record_outputs=True,
                event_names=("capability.completed",),
            ),
        )
        self._version = version
        self._action = _EchoAction()

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        validated = _Input.model_validate(payload)
        step = WorkflowStep(
            id="echo",
            action=self._action,
            inputs=lambda _: {"value": validated.value, "version": self._version},
        )
        return WorkflowDefinition(
            id="test.versioned.workflow",
            version=self._version,
            capability_id=self.metadata.id,
            stages=(WorkflowStage("run", StageMode.SEQUENTIAL, (step,)),),
            result=_result,
        )


def _result(state: StepOutputs) -> dict[str, Any]:
    return dict(state["echo"])


class _Plugin:
    def __init__(self, version: str) -> None:
        capability = _VersionedCapability(version)
        self._capabilities = (capability,)
        self.manifest = PluginManifest(
            id="test.versioned-plugin",
            version=version,
            core_api_version="2.0",
            name="Versioned test plugin",
            permissions=("test.observe",),
            os_compatibility=capability.metadata.os_compatibility,
            capabilities=(capability.metadata.id,),
        )

    def capabilities(self) -> tuple[_VersionedCapability, ...]:
        return self._capabilities


async def test_manager_selects_latest_version_and_keeps_generated_docs(
    tmp_path: Path,
) -> None:
    engine = WorkflowEngine(
        EventBus(MemoryEventSink()),
        KnowledgeGraph(tmp_path / "graph.json"),
    )
    manager = CapabilityManager(engine)
    manager.load((_Plugin("1.0.0"), _Plugin("2.0.0")))
    manager.seal()

    metadata = manager.get("test.versioned-capability")
    assert metadata is not None
    assert metadata.version == "2.0.0"
    assert [item.metadata.version for item in manager.versions("test.versioned-capability")] == [
        "2.0.0",
        "1.0.0",
    ]
    descriptor = manager.descriptor("test.versioned-capability")
    assert descriptor is not None
    assert descriptor.input_schema["required"] == ["value"]
    assert descriptor.output_schema["required"] == ["value", "version"]

    execution = await manager.execute("test.versioned-capability", {"value": 7})
    assert execution.result == {"value": 7, "version": "2.0.0"}


class _EntryPoints:
    def __init__(self, items: tuple[object, ...]) -> None:
        self.items = items

    def select(self, *, group: str) -> tuple[object, ...]:
        assert group == "ares.capabilities"
        return self.items


class _EntryPoint:
    name = "signed-test-plugin"

    def __init__(self, plugin: _Plugin) -> None:
        self.plugin = plugin

    def load(self) -> _Plugin:
        return self.plugin


def test_discovery_loads_only_allowlisted_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ares.capabilities import discovery

    plugin = _Plugin("1.0.0")
    monkeypatch.setattr(
        discovery,
        "entry_points",
        lambda: _EntryPoints((_EntryPoint(plugin),)),
    )

    assert discover_plugins(allowed_entry_points=("signed-test-plugin",)) == (plugin,)
    assert discover_plugins(allowed_entry_points=()) == ()
    with pytest.raises(PluginDiscoveryError, match="unavailable"):
        discover_plugins(allowed_entry_points=("missing-plugin",))
