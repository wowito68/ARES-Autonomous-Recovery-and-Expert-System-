from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


graph = Path("backend/src/ares/knowledge/graph.py")
text = graph.read_text(encoding="utf-8")
if "    FIRMWARE = \"firmware\"" not in text:
    marker = "class GraphKind(StrEnum):\n"
    if marker not in text:
        raise SystemExit("GraphKind marker missing")
    additions = (
        "    FIRMWARE = \"firmware\"\n"
        "    BOOT_ENTRY = \"boot_entry\"\n"
        "    BOOTLOADER = \"bootloader\"\n"
        "    BOOT_CONFIGURATION = \"boot_configuration\"\n"
        "    KERNEL = \"kernel\"\n"
        "    INITRAMFS = \"initramfs\"\n"
        "    BOOT_REPAIR = \"boot_repair\"\n"
        "    BOOT_VERIFICATION = \"boot_verification\"\n"
    )
    graph.write_text(text.replace(marker, marker + additions, 1), encoding="utf-8")

router = Path("backend/src/ares/api/router.py")
text = router.read_text(encoding="utf-8")
if "from ares.api.routes.boot import router as boot_router" not in text:
    text = text.replace(
        "from fastapi import APIRouter\n",
        "from fastapi import APIRouter\n\nfrom ares.api.routes.boot import router as boot_router\n",
        1,
    )
if "api_router.include_router(boot_router)" not in text:
    marker = "api_router = APIRouter()\n"
    if marker not in text:
        raise SystemExit("api router marker missing")
    text = text.replace(marker, marker + "api_router.include_router(boot_router)\n", 1)
router.write_text(text, encoding="utf-8")

main = Path("backend/src/ares/main.py")
text = main.read_text(encoding="utf-8")
if "from ares.boot import (" not in text:
    marker = "from ares.backup import (\n"
    if marker not in text:
        raise SystemExit("main backup import marker missing")
    boot_import = (
        "from ares.boot import (\n"
        "    BootRecoveryEngine,\n"
        "    BootRecoveryService,\n"
        "    BootRecoveryStore,\n"
        "    BootRepairExecutor,\n"
        "    LocalTestBootExecutor,\n"
        "    UnixBrokerBootExecutor,\n"
        ")\n"
    )
    text = text.replace(marker, boot_import + marker, 1)
if "from ares.capabilities.plugins.boot_recovery import BootRecoveryPlugin" not in text:
    marker = "from ares.capabilities.plugins.backup import BackupPlugin\n"
    if marker not in text:
        raise SystemExit("main backup plugin import marker missing")
    text = text.replace(
        marker,
        marker + "from ares.capabilities.plugins.boot_recovery import BootRecoveryPlugin\n",
        1,
    )
if "from ares.tools.boot import BootRepairToolSuite" not in text:
    marker = "from ares.tools.backup import BackupFilesystemTools\n"
    if marker not in text:
        raise SystemExit("main backup tools marker missing")
    text = text.replace(marker, marker + "from ares.tools.boot import BootRepairToolSuite\n", 1)

if "boot_recovery_store = BootRecoveryStore" not in text:
    marker = "    storage_operation_store = StorageOperationStore(capability_state_dir / \"storage-operations\")\n"
    if marker not in text:
        raise SystemExit("storage operation store marker missing")
    text = text.replace(
        marker,
        marker + "    boot_recovery_store = BootRecoveryStore(capability_state_dir / \"boot\")\n",
        1,
    )
if "    boot_executor: BootRepairExecutor\n" not in text:
    marker = "    storage_operation_executor: StorageOperationExecutor\n"
    if marker not in text:
        raise SystemExit("storage executor annotation marker missing")
    text = text.replace(marker, marker + "    boot_executor: BootRepairExecutor\n", 1)

if "LocalTestBootExecutor(" not in text:
    marker = "        storage_operation_executor = LocalTestStorageExecutor(storage_partition_tools)\n"
    if marker not in text:
        raise SystemExit("local storage executor marker missing")
    local = (
        "        boot_tools = BootRepairToolSuite(\n"
        "            test_mode=True,\n"
        "            runtime_root=capability_state_dir / \"boot/runtime\",\n"
        "        )\n"
        "        boot_executor = LocalTestBootExecutor(\n"
        "            boot_tools, capability_state_dir / \"boot/checkpoint-files\"\n"
        "        )\n"
    )
    text = text.replace(marker, marker + local, 1)
if "UnixBrokerBootExecutor(" not in text:
    marker = (
        "        storage_operation_executor = UnixBrokerStorageExecutor(\n"
        "            resolved_settings.backup_broker_socket\n"
        "        )\n"
    )
    if marker not in text:
        raise SystemExit("unix storage executor marker missing")
    production = (
        "        boot_tools = BootRepairToolSuite()\n"
        "        boot_executor = UnixBrokerBootExecutor(resolved_settings.backup_broker_socket)\n"
    )
    text = text.replace(marker, marker + production, 1)

if "    boot_recovery_engine = BootRecoveryEngine(" not in text:
    marker = "    workflow_engine = WorkflowEngine(event_bus, knowledge_graph)\n"
    if marker not in text:
        raise SystemExit("workflow engine marker missing")
    engine = (
        "    boot_recovery_engine = BootRecoveryEngine(\n"
        "        tools=boot_tools,\n"
        "        storage=storage_operation_engine,\n"
        "        store=boot_recovery_store,\n"
        "        checkpoints=checkpoint_store,\n"
        "        executor=boot_executor,\n"
        "    )\n"
    )
    text = text.replace(marker, engine + marker, 1)
if "        BootRecoveryPlugin(boot_recovery_engine),\n" not in text:
    marker = "        StoragePartitionPlugin(storage_operation_engine, storage_operation_store),\n"
    if marker not in text:
        raise SystemExit("storage plugin tuple marker missing")
    text = text.replace(marker, marker + "        BootRecoveryPlugin(boot_recovery_engine),\n", 1)
if "    boot_recovery_service = BootRecoveryService(" not in text:
    marker = "    filesystem_repair_service = FilesystemRepairService(\n"
    if marker not in text:
        raise SystemExit("filesystem service marker missing")
    service = (
        "    boot_recovery_service = BootRecoveryService(\n"
        "        engine=boot_recovery_engine,\n"
        "        capabilities=capability_manager,\n"
        "        event_bus=event_bus,\n"
        "        audit=audit_ledger,\n"
        "    )\n"
    )
    text = text.replace(marker, service + marker, 1)
if "        boot_recovery_store.prepare()\n" not in text:
    marker = "        storage_operation_store.prepare()\n"
    if marker not in text:
        raise SystemExit("storage prepare marker missing")
    text = text.replace(marker, marker + "        boot_recovery_store.prepare()\n", 1)
if "            await boot_recovery_service.shutdown()\n" not in text:
    marker = "            await storage_operation_service.shutdown()\n"
    if marker not in text:
        raise SystemExit("storage shutdown marker missing")
    text = text.replace(marker, "            await boot_recovery_service.shutdown()\n" + marker, 1)
if "    application.state.boot_recovery_service = boot_recovery_service\n" not in text:
    marker = "    application.state.storage_operation_service = storage_operation_service\n"
    if marker not in text:
        raise SystemExit("storage state marker missing")
    states = (
        "    application.state.boot_recovery_store = boot_recovery_store\n"
        "    application.state.boot_tools = boot_tools\n"
        "    application.state.boot_executor = boot_executor\n"
        "    application.state.boot_recovery_engine = boot_recovery_engine\n"
        "    application.state.boot_recovery_service = boot_recovery_service\n"
    )
    text = text.replace(marker, marker + states, 1)
main.write_text(text, encoding="utf-8")
