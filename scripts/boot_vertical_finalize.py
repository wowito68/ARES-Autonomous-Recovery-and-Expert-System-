from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# API router
replace_once(
    "backend/src/ares/api/router.py",
    "from ares.api.routes.backups import router as backups_router\n",
    "from ares.api.routes.backups import router as backups_router\nfrom ares.api.routes.boot import router as boot_router\n",
)
replace_once(
    "backend/src/ares/api/router.py",
    "api_router.include_router(backups_router, prefix=\"/backups\", tags=[\"backups\"])\n",
    "api_router.include_router(backups_router, prefix=\"/backups\", tags=[\"backups\"])\napi_router.include_router(boot_router)\n",
)

# Plugin exports
replace_once(
    "backend/src/ares/capabilities/plugins/__init__.py",
    "from ares.capabilities.plugins.backup import BackupPlugin\n",
    "from ares.capabilities.plugins.backup import BackupPlugin\nfrom ares.capabilities.plugins.boot_recovery import BootRecoveryPlugin\n",
)
replace_once(
    "backend/src/ares/capabilities/plugins/__init__.py",
    '    "BackupPlugin",\n',
    '    "BackupPlugin",\n    "BootRecoveryPlugin",\n',
)

# Composition root
replace_once(
    "backend/src/ares/main.py",
    "from ares.capabilities import CapabilityManager, discover_plugins\n",
    "from ares.boot import (\n"
    "    BootRecoveryEngine,\n"
    "    BootRecoveryService,\n"
    "    BootRecoveryStore,\n"
    "    BootRepairExecutor,\n"
    "    LocalTestBootExecutor,\n"
    "    UnixBrokerBootExecutor,\n"
    ")\n"
    "from ares.capabilities import CapabilityManager, discover_plugins\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    BackupPlugin,\n    DiskAnalysisPlugin,\n",
    "    BackupPlugin,\n    BootRecoveryPlugin,\n    DiskAnalysisPlugin,\n",
)
replace_once(
    "backend/src/ares/main.py",
    "from ares.tools.filesystem import FilesystemToolSuite, MountSafetyChecker\n",
    "from ares.tools.boot import BootRepairToolSuite\n"
    "from ares.tools.filesystem import FilesystemToolSuite, MountSafetyChecker\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    storage_operation_store = StorageOperationStore(capability_state_dir / \"storage-operations\")\n",
    "    storage_operation_store = StorageOperationStore(capability_state_dir / \"storage-operations\")\n"
    "    boot_store = BootRecoveryStore(capability_state_dir / \"boot\")\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    storage_operation_executor: StorageOperationExecutor\n",
    "    storage_operation_executor: StorageOperationExecutor\n    boot_executor: BootRepairExecutor\n",
)
replace_once(
    "backend/src/ares/main.py",
    "        storage_operation_executor = LocalTestStorageExecutor(storage_partition_tools)\n",
    "        storage_operation_executor = LocalTestStorageExecutor(storage_partition_tools)\n"
    "        boot_tools = BootRepairToolSuite(\n"
    "            test_mode=True, runtime_root=capability_state_dir / \"boot/runtime\"\n"
    "        )\n"
    "        boot_executor = LocalTestBootExecutor(\n"
    "            boot_tools, capability_state_dir / \"boot/checkpoints\"\n"
    "        )\n",
)
replace_once(
    "backend/src/ares/main.py",
    "        storage_operation_executor = UnixBrokerStorageExecutor(\n            resolved_settings.backup_broker_socket\n        )\n",
    "        storage_operation_executor = UnixBrokerStorageExecutor(\n            resolved_settings.backup_broker_socket\n        )\n"
    "        boot_tools = BootRepairToolSuite()\n"
    "        boot_executor = UnixBrokerBootExecutor(resolved_settings.backup_broker_socket)\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    workflow_engine = WorkflowEngine(event_bus, knowledge_graph)\n",
    "    boot_engine = BootRecoveryEngine(\n"
    "        tools=boot_tools,\n"
    "        storage=storage_operation_engine,\n"
    "        store=boot_store,\n"
    "        checkpoints=checkpoint_store,\n"
    "        executor=boot_executor,\n"
    "    )\n"
    "    workflow_engine = WorkflowEngine(event_bus, knowledge_graph)\n",
)
replace_once(
    "backend/src/ares/main.py",
    "        StoragePartitionPlugin(storage_operation_engine, storage_operation_store),\n",
    "        StoragePartitionPlugin(storage_operation_engine, storage_operation_store),\n"
    "        BootRecoveryPlugin(boot_engine),\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    filesystem_repair_service = FilesystemRepairService(\n",
    "    boot_recovery_service = BootRecoveryService(\n"
    "        engine=boot_engine,\n"
    "        capabilities=capability_manager,\n"
    "        event_bus=event_bus,\n"
    "        audit=audit_ledger,\n"
    "    )\n"
    "    filesystem_repair_service = FilesystemRepairService(\n",
)
replace_once(
    "backend/src/ares/main.py",
    "        storage_operation_store.prepare()\n",
    "        storage_operation_store.prepare()\n        boot_store.prepare()\n",
)
replace_once(
    "backend/src/ares/main.py",
    "            await storage_operation_service.shutdown()\n",
    "            await boot_recovery_service.shutdown()\n            await storage_operation_service.shutdown()\n",
)
replace_once(
    "backend/src/ares/main.py",
    "    application.state.storage_operation_service = storage_operation_service\n",
    "    application.state.storage_operation_service = storage_operation_service\n"
    "    application.state.boot_recovery_store = boot_store\n"
    "    application.state.boot_tools = boot_tools\n"
    "    application.state.boot_executor = boot_executor\n"
    "    application.state.boot_engine = boot_engine\n"
    "    application.state.boot_recovery_service = boot_recovery_service\n",
)
replace_once(
    "backend/src/ares/main.py",
    "                '<script src=\"/filesystem.js\" defer></script>',\n",
    "                '<script src=\"/filesystem.js\" defer></script>',\n"
    "                '<script src=\"/boot.js\" defer></script>',\n",
)

# Consent authority
replace_once(
    "backend/src/ares/runtime/consent.py",
    "from ares.backup.models import BackupPlan\n",
    "from ares.backup.models import BackupPlan\nfrom ares.boot.models import BootRepairPlan\n",
)
replace_once(
    "backend/src/ares/runtime/consent.py",
    "        if action == \"storage.create\":\n            self._require_peer(peer_uid, self.broker_uid)\n            return await self._create_storage(request)\n",
    "        if action == \"storage.create\":\n"
    "            self._require_peer(peer_uid, self.broker_uid)\n"
    "            return await self._create_storage(request)\n"
    "        if action == \"boot.create\":\n"
    "            self._require_peer(peer_uid, self.broker_uid)\n"
    "            return await self._create_boot(request)\n",
)
anchor = "    async def _store_and_audit_requested(\n"
boot_method = '''    async def _create_boot(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = BootRepairPlan.model_validate(request.get("plan"))
        checkpoint = plan.protection_checkpoint
        if checkpoint is None:
            raise ValueError("boot checkpoint required")
        challenge = _Challenge(
            id=uuid4().hex,
            kind="boot_repair",
            plan_id=plan.id,
            fingerprint=plan.fingerprint_sha256,
            session_id=plan.session_id,
            correlation_id=plan.repair_id,
            capability_id="boot.repair.grub",
            risk=plan.risk,
            confirmation_phrase=(
                "I understand that this operation modifies boot state. "
                f"APPROVE {plan.fingerprint_sha256[:12]}"
            ),
            public_payload={
                "repair_id": plan.repair_id,
                "target_os": plan.target_os.id,
                "target": plan.target_disk.canonical_path,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "firmware_mode": plan.boot_impact.firmware_mode,
                "bootloader": plan.bootloader.kind.value,
                "root_path": plan.target_os.root_path,
                "esp": plan.target_esp.resource_id if plan.target_esp else None,
                "checkpoint_id": checkpoint.id,
                "operations": [item.kind.value for item in plan.operations if item.enabled],
                "issue_codes": [item.code.value for item in plan.issues],
                "limitations": list(plan.limitations),
                "offline_verification_reboot_proof": False,
            },
            expires_at=min(plan.expires_at, datetime.now(UTC) + timedelta(minutes=10)),
        )
        await self._store_and_audit_requested(
            challenge,
            {
                "repair_id": plan.repair_id,
                "target_fingerprint": plan.target_disk.fingerprint_sha256,
                "checkpoint_id": checkpoint.id,
                "operation_count": len(plan.operations),
            },
        )
        return challenge.public()

'''
replace_once("backend/src/ares/runtime/consent.py", anchor, boot_method + anchor)
replace_once(
    "backend/src/ares/runtime/consent.py",
    "    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]:\n",
    "    async def request_boot(self, plan: BootRepairPlan) -> dict[str, Any]:\n"
    "        return await _request(\n"
    "            self.socket_path,\n"
    "            {\"action\": \"boot.create\", \"plan\": plan.model_dump(mode=\"json\")},\n"
    "            timeout_seconds=3.0,\n"
    "        )\n\n"
    "    async def wait(self, challenge_id: str, timeout_seconds: float = 600.0) -> dict[str, Any]:\n",
)
replace_once(
    "backend/src/ares/runtime/consent.py",
    "    if challenge.kind == \"storage_operation\":\n        return \"storage\"\n    return \"repair\"\n",
    "    if challenge.kind == \"storage_operation\":\n        return \"storage\"\n"
    "    if challenge.kind == \"boot_repair\":\n        return \"boot.repair\"\n"
    "    return \"repair\"\n",
)

# Root broker multiplexing
replace_once(
    "backend/src/ares/runtime/broker.py",
    "from ares.backup.models import (\n",
    "from ares.boot import BootRecoveryStore\n"
    "from ares.runtime.boot_broker import BootBroker\n"
    "from ares.backup.models import (\n",
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    "from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite\n",
    "from ares.tools.boot import BootRepairToolSuite, BootToolError\n"
    "from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite\n",
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    "from ares.tools.partition import PartitionToolError, StoragePartitionToolSuite\n",
    "from ares.tools.partition import (\n"
    "    DiskIdentityTool,\n"
    "    PartitionToolError,\n"
    "    StoragePartitionToolSuite,\n"
    ")\n",
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    "    storage_broker = StorageBroker(\n",
    "    boot_store = BootRecoveryStore(Path(\"/var/lib/ares/capabilities/boot\"))\n"
    "    boot_store.prepare()\n"
    "    boot_broker = BootBroker(\n"
    "        BootRepairToolSuite(),\n"
    "        DiskIdentityTool(),\n"
    "        audit,\n"
    "        consent,\n"
    "        checkpoint_store,\n"
    "        boot_store,\n"
    "    )\n"
    "    storage_broker = StorageBroker(\n",
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    "            elif isinstance(action, str) and action.startswith(\"storage.\"):\n                result = await storage_broker.dispatch(request, peer_uid, send)\n            else:\n",
    "            elif isinstance(action, str) and action.startswith(\"storage.\"):\n"
    "                result = await storage_broker.dispatch(request, peer_uid, send)\n"
    "            elif isinstance(action, str) and action.startswith(\"boot.\"):\n"
    "                result = await boot_broker.dispatch(request, peer_uid, send)\n"
    "            else:\n",
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    "    if isinstance(exc, PartitionToolError):\n        return exc.code\n",
    "    if isinstance(exc, PartitionToolError):\n        return exc.code\n"
    "    if isinstance(exc, BootToolError):\n        return exc.code\n",
)

# CLI
replace_once(
    "backend/src/ares/cli.py",
    "from ares.config import get_settings\n",
    "from ares.boot import BootRecoveryService, BootRecoveryServiceError, BootRepairStatus\n"
    "from ares.boot.service import BootDiagnoseRequest, BootRepairPlanRequest, BootRepairRequest\n"
    "from ares.config import get_settings\n",
)
replace_once(
    "backend/src/ares/cli.py",
    "_TERMINAL_REPAIR_STATES = {\n",
    "_TERMINAL_BOOT_STATES = {\n"
    "    BootRepairStatus.COMPLETED,\n"
    "    BootRepairStatus.REPAIR_FAILED,\n"
    "    BootRepairStatus.ABORTED,\n"
    "    BootRepairStatus.UNKNOWN,\n"
    "}\n"
    "_TERMINAL_REPAIR_STATES = {\n",
)
parser_anchor = "    consent = commands.add_parser(\"consent\", help=\"Trusted local approval channel\")\n"
boot_parser = '''    boot = commands.add_parser("boot", help="Diagnose and recover Linux boot state")
    boot_commands = boot.add_subparsers(dest="boot_command", required=True)
    boot_diagnose = boot_commands.add_parser("diagnose", help="Run read-only boot diagnosis")
    boot_diagnose.add_argument("--target-disk")
    boot_diagnose.add_argument("--root-path")
    boot_plan = boot_commands.add_parser("plan", help="Build an evidence-bound boot repair plan")
    boot_plan.add_argument("diagnostic_id")
    boot_plan.add_argument("--target-os-id")
    boot_repair = boot_commands.add_parser("repair", help="Protect and request boot repair")
    boot_repair.add_argument("plan_id")
    boot_status = boot_commands.add_parser("status", help="Read durable boot repair state")
    boot_status.add_argument("repair_id")
    boot_verify = boot_commands.add_parser("verify", help="Read boot verification evidence")
    boot_verify.add_argument("repair_id")
    boot_cancel = boot_commands.add_parser("cancel", help="Cancel a non-terminal boot repair")
    boot_cancel.add_argument("repair_id")
    boot_reconcile = boot_commands.add_parser("reconcile", help="Reinspect UNKNOWN boot state")
    boot_reconcile.add_argument("repair_id")

'''
replace_once("backend/src/ares/cli.py", parser_anchor, boot_parser + parser_anchor)
replace_once(
    "backend/src/ares/cli.py",
    "        if args.command == \"filesystem\":\n            return await _filesystem_command(args, application)\n",
    "        if args.command == \"filesystem\":\n            return await _filesystem_command(args, application)\n"
    "        if args.command == \"boot\":\n            return await _boot_command(args, application)\n",
)
consent_anchor = "\n\nasync def _consent_command(args: argparse.Namespace) -> int:\n"
boot_handler = '''

async def _boot_command(args: argparse.Namespace, application: FastAPI) -> int:
    service = cast(BootRecoveryService, application.state.boot_recovery_service)
    session_id = f"cli-{uuid4().hex}"
    try:
        if args.boot_command == "diagnose":
            result = await service.diagnose(
                BootDiagnoseRequest(
                    target_disk=cast(str | None, args.target_disk),
                    root_path=cast(str | None, args.root_path),
                ),
                session_id=session_id,
            )
            print(result.model_dump_json(indent=2))
            return 0
        if args.boot_command == "plan":
            result = await service.plan(
                BootRepairPlanRequest(
                    diagnostic_id=cast(str, args.diagnostic_id),
                    target_os_id=cast(str | None, args.target_os_id),
                ),
                session_id=session_id,
            )
            print(result.model_dump_json(indent=2))
            return 0 if result.executable else 3
        if args.boot_command == "repair":
            stored = await service.engine.store.get_plan(cast(str, args.plan_id))
            if stored is None:
                print("Boot repair plan not found.", file=sys.stderr)
                return 3
            print("BOOT REPAIR PLAN")
            print(stored.model_dump_json(indent=2))
            if not stored.executable:
                print("Boot repair plan is blocked by safety limitations.", file=sys.stderr)
                return 4
            confirmation = await asyncio.to_thread(
                input,
                "Type REQUEST to create the boot checkpoint and request independent authorization: ",
            )
            if confirmation != "REQUEST":
                print("Boot repair request cancelled before authorization.", file=sys.stderr)
                return 4
            accepted = await service.start(
                BootRepairRequest(plan_id=stored.id, request_authorization=True),
                session_id=stored.session_id,
                created_by="local-cli-user",
            )
            print(accepted.model_dump_json(indent=2))
            repair_id = accepted.repair.execution.repair_id
            while True:
                record = await service.get(repair_id)
                if record is None:
                    print("Boot repair record disappeared.", file=sys.stderr)
                    return 5
                challenge = record.execution.authorization_challenge_id
                if challenge:
                    print(
                        "Independent authorization required. Run locally:\n"
                        f"  ares consent approve {challenge}",
                        file=sys.stderr,
                    )
                print(
                    json.dumps(
                        {
                            "repair_id": repair_id,
                            "status": record.execution.status.value,
                            "stage": record.execution.last_known_stage,
                            "error_code": record.execution.error_code,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                if record.execution.status in _TERMINAL_BOOT_STATES:
                    print(record.model_dump_json(indent=2))
                    return 0 if record.execution.status is BootRepairStatus.COMPLETED else 6
                await asyncio.sleep(0.5)
        repair_id = cast(str, args.repair_id)
        record = await service.get(repair_id)
        if args.boot_command == "status":
            if record is None:
                print("Boot repair not found.", file=sys.stderr)
                return 3
            print(record.model_dump_json(indent=2))
            return 0
        if args.boot_command == "verify":
            verification = await service.verification(repair_id)
            if verification is None:
                print("Boot verification not found.", file=sys.stderr)
                return 3
            print(verification.model_dump_json(indent=2))
            return 0
        if record is None:
            print("Boot repair not found.", file=sys.stderr)
            return 3
        if args.boot_command == "cancel":
            cancelled = await service.cancel(repair_id, session_id=record.execution.session_id)
            print(cancelled.model_dump_json(indent=2))
            return 0
        if args.boot_command == "reconcile":
            result = await service.reconcile(repair_id, session_id=record.execution.session_id)
            print(result.model_dump_json(indent=2))
            return 0
    except BootRecoveryServiceError as exc:
        print(f"ARES boot recovery failed: {exc.code}", file=sys.stderr)
        return 2
    return 1
'''
replace_once("backend/src/ares/cli.py", consent_anchor, boot_handler + consent_anchor)

# Agent contract
replace_once(
    "backend/src/ares/api/routes/assistant.py",
    "StorageTransaction UNKNOWN exige reinspección y nunca reintento automático.\n\n",
    "StorageTransaction UNKNOWN exige reinspección y nunca reintento automático.\n\n"
    "Conoces boot.diagnose y boot.repair.grub. Ante 'mi Linux no arranca' debes comenzar por "
    "diagnóstico read-only y evidencia de firmware, bootloader, ESP, kernel, initramfs y root; "
    "nunca por reinstalar GRUB. La reparación automática se limita a GRUB en sistemas Debian-family "
    "y exige BootRepairPlan exacto, checkpoint de estado de arranque, consentimiento local "
    "independiente y broker root. Windows Boot Manager y systemd-boot se detectan pero no se reparan "
    "automáticamente. fstab se analiza pero no se modifica desde Boot Recovery. Una verificación "
    "offline no prueba un reinicio real: si falta esa evidencia, reporta PARTIAL/LIMITED y no "
    "afirmes que el sistema ya arranca. Un estado UNKNOWN exige reconciliación, no reintento.\n\n",
)

# systemd sandbox and tmpfiles
replace_once(
    "live/config/includes.chroot/usr/lib/systemd/system/ares-tool-broker.service",
    "ReadWritePaths=/run/ares/sockets /var/lib/ares/broker /var/lib/ares/backups /var/lib/ares/storage-images /mnt /media /run/media",
    "ReadWritePaths=/run/ares/sockets /var/lib/ares/broker /var/lib/ares/backups /var/lib/ares/storage-images /var/lib/ares/boot /var/lib/ares/capabilities/boot /mnt /media /run/media",
)
replace_once(
    "live/config/includes.chroot/usr/lib/tmpfiles.d/ares.conf",
    "d /var/lib/ares/backups 0750 root ares -\n",
    "d /var/lib/ares/backups 0750 root ares -\n"
    "d /var/lib/ares/boot 0750 root ares-broker -\n"
    "d /var/lib/ares/boot/checkpoints 0700 root ares-broker -\n"
    "d /var/lib/ares/capabilities/boot 0700 ares-api ares-api -\n",
)

# UI syntax gate
replace_once(
    ".github/workflows/backend-ci.yml",
    "          node --check live/config/includes.chroot/usr/share/ares/platform/partition.js\n",
    "          node --check live/config/includes.chroot/usr/share/ares/platform/partition.js\n"
    "          node --check live/config/includes.chroot/usr/share/ares/platform/boot.js\n",
)
