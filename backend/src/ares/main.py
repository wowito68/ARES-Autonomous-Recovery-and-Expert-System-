"""FastAPI composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ares.api.router import api_router
from ares.agent import AgentOrchestrator, AgentRunStore
from ares.audit import AuditLedger, MemoryAuditLedger, UnixAuditLedgerClient
from ares.backup import (
    BackupExecutor,
    BackupService,
    BackupStore,
    LocalTestBackupExecutor,
    UnixBrokerBackupExecutor,
)
from ares.capabilities import CapabilityManager, discover_plugins
from ares.capabilities.plugins import (
    BackupPlugin,
    BootDiagnosticsPlugin,
    DiskAnalysisPlugin,
    FilesystemRepairPlugin,
    StoragePartitionPlugin,
)
from ares.config import Environment, Settings, get_settings
from ares.core.logging import configure_logging
from ares.core.middleware import RequestContextMiddleware
from ares.core.problems import install_problem_handlers
from ares.database import Database
from ares.diagnostics import DiagnosticStore
from ares.events import EventBus, JsonlEventSink
from ares.filesystems.executor import (
    FilesystemExecutor,
    LocalTestFilesystemExecutor,
    UnixBrokerFilesystemExecutor,
)
from ares.filesystems.service import FilesystemRepairService
from ares.filesystems.store import FilesystemRepairStore
from ares.knowledge import KnowledgeGraph
from ares.llm import AIRuntime, OllamaRuntime
from ares.planner import Planner
from ares.protection import ProtectionCheckpointService, ProtectionCheckpointStore
from ares.reasoning import ReasoningEngine
from ares.resources.service import ResourceResolver
from ares.session import SystemSessionService
from ares.storage.service import StorageAnalysisService
from ares.storage.store import StorageSnapshotStore
from ares.storage_operations import (
    LocalTestStorageExecutor,
    ProductionStorageWriteGate,
    StorageOperationEngine,
    StorageOperationExecutor,
    StorageOperationService,
    StorageOperationStore,
    UnixBrokerStorageExecutor,
)
from ares.terminal import (
    LocalTestTerminalExecutor,
    TerminalService,
    TerminalStore,
    UnixBrokerTerminalExecutor,
)
from ares.tools import (
    BackupFilesystemTools,
    ReadOnlyStorageProcessRunner,
    SafeProcessRunner,
    StorageToolSuite,
)
from ares.tools.filesystem import FilesystemToolSuite, MountSafetyChecker
from ares.tools.partition import StoragePartitionToolSuite
from ares.workflows import WorkflowEngine


def create_app(
    settings: Settings | None = None,
    *,
    ai_runtime: AIRuntime | None = None,
) -> FastAPI:
    """Build an isolated application instance for production or tests."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    database = Database(
        resolved_settings.database_url,
        echo=resolved_settings.database_echo,
        busy_timeout_ms=resolved_settings.database_busy_timeout_ms,
        require_existing_file=resolved_settings.environment is Environment.PRODUCTION,
    )
    capability_state_dir = resolved_settings.capability_state_dir
    if capability_state_dir is None:
        capability_state_dir = (
            database.database_path.parent / "capabilities"
            if database.database_path is not None
            else resolved_settings.runtime_state_dir / "capabilities"
        )
    inventory_path = resolved_settings.runtime_state_dir / "hardware/public/inventory-v1.json"
    event_bus = EventBus(JsonlEventSink(capability_state_dir / "events.jsonl"))
    knowledge_graph = KnowledgeGraph(capability_state_dir / "knowledge-graph.json")
    snapshot_store = StorageSnapshotStore(capability_state_dir / "storage/snapshots")
    diagnostic_store = DiagnosticStore(capability_state_dir / "diagnostics")
    backup_store = BackupStore(capability_state_dir / "backups")
    checkpoint_store = ProtectionCheckpointStore(capability_state_dir / "protection/checkpoints")
    filesystem_store = FilesystemRepairStore(capability_state_dir / "filesystems")
    storage_operation_store = StorageOperationStore(capability_state_dir / "storage-operations")
    agent_run_store = AgentRunStore(capability_state_dir / "agent/runs")
    terminal_store = TerminalStore(capability_state_dir / "terminal")
    storage_write_gate = ProductionStorageWriteGate(
        test_mode=resolved_settings.environment is Environment.TEST
    )
    storage_runner = ReadOnlyStorageProcessRunner(SafeProcessRunner())
    storage_tools = StorageToolSuite(
        inventory_path,
        runner=storage_runner,
        process_probes_enabled=resolved_settings.storage_process_probes_enabled,
    )
    backup_tools = BackupFilesystemTools()
    backup_executor: BackupExecutor
    audit_ledger: AuditLedger
    filesystem_executor: FilesystemExecutor
    storage_operation_executor: StorageOperationExecutor
    if resolved_settings.environment is Environment.TEST:
        backup_executor = LocalTestBackupExecutor(backup_tools)
        audit_ledger = MemoryAuditLedger()
        filesystem_tools = FilesystemToolSuite(
            mount_checker=MountSafetyChecker(scan_processes=False),
            allow_regular_file_targets=True,
        )
        filesystem_executor = LocalTestFilesystemExecutor(filesystem_tools)
        storage_partition_tools = StoragePartitionToolSuite(
            allow_regular_file_targets=True,
            write_gate=storage_write_gate,
        )
        storage_operation_executor = LocalTestStorageExecutor(storage_partition_tools)
        terminal_executor = LocalTestTerminalExecutor()
    else:
        backup_executor = UnixBrokerBackupExecutor(resolved_settings.backup_broker_socket)
        audit_ledger = UnixAuditLedgerClient(resolved_settings.audit_socket)
        filesystem_tools = None
        filesystem_executor = UnixBrokerFilesystemExecutor(resolved_settings.backup_broker_socket)
        storage_partition_tools = None
        storage_operation_executor = UnixBrokerStorageExecutor(
            resolved_settings.backup_broker_socket
        )
        terminal_executor = UnixBrokerTerminalExecutor(resolved_settings.backup_broker_socket)
    storage_operation_engine = StorageOperationEngine(
        executor=storage_operation_executor,
        store=storage_operation_store,
        checkpoints=checkpoint_store,
        write_gate=storage_write_gate,
    )
    workflow_engine = WorkflowEngine(event_bus, knowledge_graph)
    capability_manager = CapabilityManager(
        workflow_engine,
        live_mode=_read_live_mode(resolved_settings),
    )
    builtins = (
        DiskAnalysisPlugin(storage_tools, snapshot_store),
        BootDiagnosticsPlugin(snapshot_store),
        BackupPlugin(backup_store, backup_tools, backup_executor),
        FilesystemRepairPlugin(filesystem_store, filesystem_executor),
        StoragePartitionPlugin(storage_operation_engine, storage_operation_store),
    )
    capability_manager.load(
        discover_plugins(
            builtins,
            entry_point_group=resolved_settings.capability_plugin_entrypoint_group,
            allowed_entry_points=resolved_settings.capability_plugin_allowlist,
        )
    )
    capability_manager.seal()
    reasoning_engine = ReasoningEngine(capability_manager)
    planner = Planner(reasoning_engine, capability_manager)
    storage_analysis_service = StorageAnalysisService(
        capability_manager,
        reasoning_engine,
        snapshot_store,
        diagnostic_store,
        event_bus,
        storage_tools,
        inventory_path,
    )
    resource_resolver = ResourceResolver(snapshot_store)
    terminal_service = TerminalService(
        settings=resolved_settings,
        resources=resource_resolver,
        store=terminal_store,
        executor=terminal_executor,
        audit=audit_ledger,
    )
    system_session_service = SystemSessionService(
        resolved_settings, resource_resolver, terminal_service
    )
    agent_orchestrator = AgentOrchestrator(
        store=agent_run_store,
        resources=resource_resolver,
        planner=planner,
        capabilities=capability_manager,
        storage=storage_analysis_service,
        events=event_bus,
    )
    backup_service = BackupService(
        capability_manager,
        workflow_engine,
        backup_store,
        backup_tools,
        event_bus,
        audit_ledger,
    )
    protection_service = ProtectionCheckpointService(backup_store, checkpoint_store)
    storage_operation_service = StorageOperationService(
        engine=storage_operation_engine,
        store=storage_operation_store,
        capabilities=capability_manager,
        event_bus=event_bus,
        audit=audit_ledger,
    )
    filesystem_repair_service = FilesystemRepairService(
        capabilities=capability_manager,
        executor=filesystem_executor,
        store=filesystem_store,
        protection=protection_service,
        event_bus=event_bus,
        audit=audit_ledger,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.prepare_storage()
        event_bus.prepare()
        knowledge_graph.prepare()
        snapshot_store.prepare()
        diagnostic_store.prepare()
        backup_store.prepare()
        checkpoint_store.prepare()
        filesystem_store.prepare()
        storage_operation_store.prepare()
        agent_run_store.prepare()
        terminal_store.prepare()
        try:
            yield
        finally:
            await storage_operation_service.shutdown()
            await filesystem_repair_service.shutdown()
            await backup_service.shutdown()
            await database.dispose()

    application = FastAPI(
        title="ARES API",
        version=resolved_settings.version,
        description="Local control plane for ARES.",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=(
            "/openapi.json" if resolved_settings.environment is not Environment.PRODUCTION else None
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.ai_runtime = ai_runtime or OllamaRuntime(resolved_settings)
    application.state.event_bus = event_bus
    application.state.audit_ledger = audit_ledger
    application.state.knowledge_graph = knowledge_graph
    application.state.workflow_engine = workflow_engine
    application.state.capability_manager = capability_manager
    application.state.reasoning_engine = reasoning_engine
    application.state.planner = planner
    application.state.storage_snapshot_store = snapshot_store
    application.state.diagnostic_store = diagnostic_store
    application.state.storage_tools = storage_tools
    application.state.storage_analysis_service = storage_analysis_service
    application.state.resource_resolver = resource_resolver
    application.state.system_session_service = system_session_service
    application.state.agent_run_store = agent_run_store
    application.state.agent_orchestrator = agent_orchestrator
    application.state.terminal_store = terminal_store
    application.state.terminal_executor = terminal_executor
    application.state.terminal_service = terminal_service
    application.state.backup_store = backup_store
    application.state.backup_tools = backup_tools
    application.state.backup_executor = backup_executor
    application.state.backup_service = backup_service
    application.state.protection_checkpoint_store = checkpoint_store
    application.state.protection_checkpoint_service = protection_service
    application.state.filesystem_repair_store = filesystem_store
    application.state.filesystem_executor = filesystem_executor
    application.state.filesystem_tools = filesystem_tools
    application.state.filesystem_repair_service = filesystem_repair_service
    application.state.storage_operation_store = storage_operation_store
    application.state.storage_write_gate = storage_write_gate
    application.state.storage_partition_tools = storage_partition_tools
    application.state.storage_operation_executor = storage_operation_executor
    application.state.storage_operation_engine = storage_operation_engine
    application.state.storage_operation_service = storage_operation_service
    application.add_middleware(RequestContextMiddleware)
    install_problem_handlers(application)
    application.include_router(api_router, prefix=resolved_settings.api_prefix)
    if resolved_settings.static_dir is not None and resolved_settings.static_dir.is_dir():
        index_path = resolved_settings.static_dir / "index.html"

        @application.get("/", include_in_schema=False, response_class=HTMLResponse)
        async def platform_index() -> HTMLResponse:
            source = index_path.read_text(encoding="utf-8")
            loaders = (
                '<script src="/backup.js" defer></script>',
                '<script src="/filesystem.js" defer></script>',
                '<script src="/partition.js" defer></script>',
            )
            for loader in loaders:
                if loader not in source:
                    source = source.replace("</body>", f"  {loader}\n</body>")
            return HTMLResponse(source)

        application.mount(
            "/",
            StaticFiles(directory=resolved_settings.static_dir, html=True),
            name="ares-interface",
        )
    return application


def _read_live_mode(settings: Settings) -> str:
    try:
        mode = (settings.runtime_state_dir / "mode").read_text(encoding="ascii").strip()
    except OSError:
        return "live"
    return mode if mode in {"live", "persistent", "forensic", "recovery"} else "live"
