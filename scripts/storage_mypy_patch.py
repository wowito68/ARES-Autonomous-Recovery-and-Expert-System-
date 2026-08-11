from pathlib import Path


def replace(path: str, old: str, new: str, *, count: int = -1) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        if new in text:
            return
        raise SystemExit(f"expected block not found: {path}: {old[:80]!r}")
    file.write_text(text.replace(old, new, count), encoding="utf-8")


# backup tool datetime typing
replace(
    "backend/src/ares/tools/backup.py",
    "from dataclasses import dataclass\nfrom pathlib import Path\n",
    "from dataclasses import dataclass\nfrom datetime import UTC, datetime\nfrom pathlib import Path\n",
)
replace(
    "backend/src/ares/tools/backup.py",
    "def _utc_now():\n    from datetime import UTC, datetime\n\n    return datetime.now(UTC)\n",
    "def _utc_now() -> datetime:\n    return datetime.now(UTC)\n",
)

# peer credentials are ints, not Any.
for path in (
    "backend/src/ares/audit/ledger.py",
    "backend/src/ares/runtime/consent.py",
    "backend/src/ares/runtime/broker.py",
):
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    text = text.replace("    return uid\n", "    return int(uid)\n")
    file.write_text(text, encoding="utf-8")

# Adapter tuple should be typed by the protocol, not concrete first element inference.
replace(
    "backend/src/ares/filesystems/adapters.py",
    "_ADAPTERS = (\n",
    "_ADAPTERS: tuple[FilesystemAdapter, ...] = (\n",
)

# Mount checker is a structural boundary so deterministic fakes type-check.
replace(
    "backend/src/ares/tools/filesystem.py",
    "class MountSafetyChecker:\n",
    "class MountSafetyInspector(Protocol):\n"
    "    def inspect(self, identity: DeviceIdentity) -> MountSafetyReport: ...\n\n\n"
    "class MountSafetyChecker:\n",
)
replace(
    "backend/src/ares/tools/filesystem.py",
    "        mount_checker: MountSafetyChecker | None = None,\n",
    "        mount_checker: MountSafetyInspector | None = None,\n",
)
replace(
    "backend/src/ares/tools/filesystem.py",
    "        if len(mounts) == 1:\n            args = (\n",
    "        args: tuple[str, ...]\n        if len(mounts) == 1:\n            args = (\n",
)

# Consent and broker Unix handlers are explicit async callbacks.
replace(
    "backend/src/ares/runtime/consent.py",
    "from contextlib import suppress\n",
    "from collections.abc import Awaitable, Callable\nfrom contextlib import suppress\n",
)
replace(
    "backend/src/ares/runtime/consent.py",
    "_MAX_MESSAGE_BYTES = 512_000\n",
    "_MAX_MESSAGE_BYTES = 512_000\nUnixHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]\n",
)
replace(
    "backend/src/ares/runtime/consent.py",
    "async def _unix_server(handler, socket_path: Path) -> asyncio.AbstractServer:\n",
    "async def _unix_server(handler: UnixHandler, socket_path: Path) -> asyncio.AbstractServer:\n",
)
replace(
    "backend/src/ares/runtime/broker.py",
    "BrokerSend = Callable[[dict[str, Any]], Awaitable[None]]\n",
    "BrokerSend = Callable[[dict[str, Any]], Awaitable[None]]\n"
    "UnixHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]\n",
)
replace(
    "backend/src/ares/runtime/broker.py",
    "async def _unix_server(handler, socket_path: Path) -> asyncio.AbstractServer:\n",
    "async def _unix_server(handler: UnixHandler, socket_path: Path) -> asyncio.AbstractServer:\n",
)

# Filesystem broker send callback typing.
replace(
    "backend/src/ares/runtime/filesystem_broker.py",
    "import os\nfrom datetime import UTC, datetime, timedelta\n",
    "import os\nfrom collections.abc import Awaitable, Callable\nfrom datetime import UTC, datetime, timedelta\n",
)
replace(
    "backend/src/ares/runtime/filesystem_broker.py",
    "from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite\n\n\n",
    "from ares.tools.filesystem import FilesystemToolError, FilesystemToolSuite\n\n"
    "FilesystemBrokerSend = Callable[[dict[str, Any]], Awaitable[None]]\n\n\n",
)
replace(
    "backend/src/ares/runtime/filesystem_broker.py",
    "        send,\n    ) -> dict[str, Any]:\n",
    "        send: FilesystemBrokerSend,\n    ) -> dict[str, Any]:\n",
)
replace(
    "backend/src/ares/runtime/filesystem_broker.py",
    "    async def _authorize(self, request: dict[str, Any], send) -> dict[str, Any]:\n",
    "    async def _authorize(\n"
    "        self, request: dict[str, Any], send: FilesystemBrokerSend\n"
    "    ) -> dict[str, Any]:\n",
)
replace(
    "backend/src/ares/runtime/filesystem_broker.py",
    "    async def _execute(self, request: dict[str, Any], send) -> dict[str, Any]:\n",
    "    async def _execute(\n"
    "        self, request: dict[str, Any], send: FilesystemBrokerSend\n"
    "    ) -> dict[str, Any]:\n",
)

# Backup broker streaming callbacks.
replace(
    "backend/src/ares/runtime/broker.py",
    "from ares.backup.models import AuthorizationGrant, Backup, BackupManifest, BackupPlan\n",
    "from ares.backup.models import (\n"
    "    AuthorizationGrant,\n"
    "    Backup,\n"
    "    BackupEntry,\n"
    "    BackupManifest,\n"
    "    BackupPlan,\n"
    "    BackupProgress,\n"
    ")\n",
)
replace(
    "backend/src/ares/runtime/broker.py",
    "        async def progress(value) -> None:\n",
    "        async def progress(value: BackupProgress) -> None:\n",
)
replace(
    "backend/src/ares/runtime/broker.py",
    "        async def entry(value) -> None:\n",
    "        async def entry(value: BackupEntry) -> None:\n",
)

# Partition literals and branch-local argv typing.
replace(
    "backend/src/ares/tools/partition.py",
    "from typing import Protocol\n",
    "from typing import Literal, Protocol\n",
)
replace(
    "backend/src/ares/tools/partition.py",
    "        kind = \"regular_file\"\n",
    "        kind: Literal[\"regular_file\", \"loop\", \"block\"] = \"regular_file\"\n",
)
replace(
    "backend/src/ares/tools/partition.py",
    "        if identity.device_kind == \"regular_file\":\n            offset = partition.start_sector",
    "        args: tuple[str, ...]\n        if identity.device_kind == \"regular_file\":\n            offset = partition.start_sector",
)
replace(
    "backend/src/ares/tools/partition.py",
    "def _filesystem_resize_support(fs_type: str) -> str:\n",
    "def _filesystem_resize_support(\n"
    "    fs_type: str,\n"
    ") -> Literal[\"grow\", \"shrink_and_grow\", \"unsupported\", \"unknown\"]:\n",
)

# Engine Literal without redundant cast.
replace(
    "backend/src/ares/storage_operations/engine.py",
    "from typing import Literal, cast\n",
    "from typing import Literal\n",
)
replace(
    "backend/src/ares/storage_operations/engine.py",
    "    kind = cast(\n        Literal[\"resize_partition\", \"move_partition\"],\n        \"resize_partition\" if operation == \"resize\" else \"move_partition\",\n    )\n",
    "    kind: Literal[\"resize_partition\", \"move_partition\"] = (\n"
    "        \"resize_partition\" if operation == \"resize\" else \"move_partition\"\n"
    "    )\n",
)

# Filesystem permissions tuple must keep variadic type.
replace(
    "backend/src/ares/filesystems/service.py",
    "        permissions = (\"block-device.readwrite\",)\n",
    "        permissions: tuple[str, ...] = (\"block-device.readwrite\",)\n",
)

# Composition root declares interface types before TEST/production branching.
replace(
    "backend/src/ares/main.py",
    "from ares.audit import MemoryAuditLedger, UnixAuditLedgerClient\n",
    "from ares.audit import AuditLedger, MemoryAuditLedger, UnixAuditLedgerClient\n",
)
replace(
    "backend/src/ares/main.py",
    "    BackupService,\n",
    "    BackupExecutor,\n    BackupService,\n",
)
replace(
    "backend/src/ares/main.py",
    "from ares.filesystems.executor import LocalTestFilesystemExecutor, UnixBrokerFilesystemExecutor\n",
    "from ares.filesystems.executor import (\n"
    "    FilesystemExecutor,\n"
    "    LocalTestFilesystemExecutor,\n"
    "    UnixBrokerFilesystemExecutor,\n"
    ")\n",
)
replace(
    "backend/src/ares/main.py",
    "from ares.storage_operations import (\n",
    "from ares.storage_operations import (\n    StorageOperationExecutor,\n",
)
replace(
    "backend/src/ares/main.py",
    "    backup_tools = BackupFilesystemTools()\n    if resolved_settings.environment is Environment.TEST:\n",
    "    backup_tools = BackupFilesystemTools()\n"
    "    backup_executor: BackupExecutor\n"
    "    audit_ledger: AuditLedger\n"
    "    filesystem_executor: FilesystemExecutor\n"
    "    storage_operation_executor: StorageOperationExecutor\n"
    "    if resolved_settings.environment is Environment.TEST:\n",
)

# CLI receives FastAPI and keeps branch-local model variables distinct.
replace(
    "backend/src/ares/cli.py",
    "from typing import cast\n",
    "from typing import cast\n\nfrom fastapi import FastAPI\n",
)
replace(
    "backend/src/ares/cli.py",
    "async def _storage_command(args: argparse.Namespace, application) -> int:\n",
    "async def _storage_command(args: argparse.Namespace, application: FastAPI) -> int:\n",
)
replace(
    "backend/src/ares/cli.py",
    "async def _backup_command(args: argparse.Namespace, application) -> int:\n",
    "async def _backup_command(args: argparse.Namespace, application: FastAPI) -> int:\n",
)
replace(
    "backend/src/ares/cli.py",
    "async def _filesystem_command(args: argparse.Namespace, application) -> int:\n",
    "async def _filesystem_command(args: argparse.Namespace, application: FastAPI) -> int:\n",
)
replace(
    "backend/src/ares/cli.py",
    "            result = await service.analyze(session_id=f\"cli-{uuid4().hex}\")\n",
    "            analysis_result = await service.analyze(session_id=f\"cli-{uuid4().hex}\")\n",
)
replace(
    "backend/src/ares/cli.py",
    "        print(result.model_dump_json(indent=2))\n        return 0\n    if args.storage_command == \"snapshot\":\n",
    "        print(analysis_result.model_dump_json(indent=2))\n        return 0\n    if args.storage_command == \"snapshot\":\n",
)
replace(
    "backend/src/ares/cli.py",
    "            result = await operations.inspect(args.target, session_id=session_id)\n            print(result.model_dump_json(indent=2))\n",
    "            layout_result = await operations.inspect(args.target, session_id=session_id)\n"
    "            print(layout_result.model_dump_json(indent=2))\n",
)
replace(
    "backend/src/ares/cli.py",
    "            result = await operations.plan(request, session_id=session_id)\n            print(result.model_dump_json(indent=2))\n",
    "            operation_plan = await operations.plan(request, session_id=session_id)\n"
    "            print(operation_plan.model_dump_json(indent=2))\n",
)
replace(
    "backend/src/ares/cli.py",
    "            result = await operations.validate(args.plan_id, session_id=session_id)\n            print(result.model_dump_json(indent=2))\n",
    "            validated_plan = await operations.validate(args.plan_id, session_id=session_id)\n"
    "            print(validated_plan.model_dump_json(indent=2))\n",
)
replace(
    "backend/src/ares/cli.py",
    "            result = await operations.reconcile(args.operation_id, session_id=session_id)\n            print(result.model_dump_json(indent=2))\n",
    "            reconciliation = await operations.reconcile(args.operation_id, session_id=session_id)\n"
    "            print(reconciliation.model_dump_json(indent=2))\n",
)

# Tests: concrete models/callbacks and structural mount checker.
replace(
    "backend/tests/test_backup_tools.py",
    "    BackupExecution,\n",
    "    BackupEntry,\n    BackupExecution,\n    BackupPlan,\n",
)
replace(
    "backend/tests/test_backup_tools.py",
    "def _backup(plan) -> Backup:\n",
    "def _backup(plan: BackupPlan) -> Backup:\n",
)
replace(
    "backend/tests/test_backup_tools.py",
    "    original = backup_tools_module.os.access\n",
    "    original = os.access\n",
)
replace(
    "backend/tests/test_backup_tools.py",
    "    monkeypatch.setattr(backup_tools_module.os, \"access\", access)\n",
    "    monkeypatch.setattr(os, \"access\", access)\n",
)
replace(
    "backend/tests/test_backup_tools.py",
    "    async def on_entry(value) -> None:\n",
    "    async def on_entry(value: BackupEntry) -> None:\n",
)
file = Path("backend/tests/test_backup_tools.py")
text = file.read_text(encoding="utf-8").replace(
    "    async def noop(_) -> None:\n", "    async def noop(_: object) -> None:\n"
)
file.write_text(text, encoding="utf-8")

replace(
    "backend/tests/test_backup_environment_failures.py",
    "def _fixture(tmp_path: Path, *, destination_fs: str = \"ext4\"):\n",
    "def _fixture(\n"
    "    tmp_path: Path, *, destination_fs: str = \"ext4\"\n"
    ") -> tuple[BackupFilesystemTools, Path, Path]:\n",
)
replace(
    "backend/tests/test_backup_environment_failures.py",
    "    async def noop(_) -> None:\n",
    "    async def noop(_: object) -> None:\n",
)
replace(
    "backend/tests/test_filesystem_preflight_failures.py",
    "    FilesystemRepairPlan,\n",
    "    DeviceIdentity,\n    FilesystemRepairPlan,\n",
)
replace(
    "backend/tests/test_filesystem_preflight_failures.py",
    "    def inspect(self, identity) -> MountSafetyReport:\n",
    "    def inspect(self, identity: DeviceIdentity) -> MountSafetyReport:\n",
)
replace(
    "backend/tests/test_backup_vertical_slice.py",
    "            payload = response.json()\n",
    "            payload = cast(dict[str, object], response.json())\n",
)
for path in (
    "backend/tests/test_filesystem_failures.py",
    "backend/tests/test_filesystem_cancellation.py",
):
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    text = text.replace("cast(FilesystemRepairService, ", "")
    text = text.replace("object.__new__(FilesystemRepairService))", "object.__new__(FilesystemRepairService)")
    file.write_text(text, encoding="utf-8")
