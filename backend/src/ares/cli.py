"""ARES command-line interface backed by the same application services as the API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Literal, cast
from uuid import uuid4

from fastapi import FastAPI

from ares.backup import BackupCreateRequest, BackupPlanRequest, BackupService, BackupServiceError
from ares.backup.models import BackupStatus
from ares.boot import BootRecoveryService, BootRecoveryServiceError, BootRepairStatus
from ares.boot.service import BootDiagnoseRequest, BootRepairPlanRequest, BootRepairRequest
from ares.config import get_settings
from ares.filesystems.models import RepairExecutionStatus
from ares.filesystems.service import (
    FilesystemInspectRequest,
    FilesystemRepairPlanRequest,
    FilesystemRepairService,
    FilesystemRepairStartRequest,
    FilesystemServiceError,
)
from ares.main import create_app
from ares.runtime.consent import UnixConsentOperatorClient
from ares.storage.service import StorageAnalysisError, StorageAnalysisService
from ares.storage_operations import (
    PartitionTableType,
    StorageOperationActionRequest,
    StorageOperationPlanRequest,
    StorageOperationService,
    StorageOperationServiceError,
)

_TERMINAL_BACKUP_STATES = {
    BackupStatus.COMPLETED,
    BackupStatus.FAILED,
    BackupStatus.CANCELLED,
    BackupStatus.CORRUPTED,
}
_TERMINAL_BOOT_STATES = {
    BootRepairStatus.COMPLETED,
    BootRepairStatus.REPAIR_FAILED,
    BootRepairStatus.ABORTED,
    BootRepairStatus.UNKNOWN,
}
_TERMINAL_REPAIR_STATES = {
    RepairExecutionStatus.COMPLETED,
    RepairExecutionStatus.PARTIAL,
    RepairExecutionStatus.FAILED,
    RepairExecutionStatus.CANCELLED,
    RepairExecutionStatus.ABORTED,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ares")
    commands = parser.add_subparsers(dest="command", required=True)

    storage = commands.add_parser("storage", help="Read-only storage diagnostics")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("analyze", help="Run storage.disk-analysis")
    snapshot = storage_commands.add_parser("snapshot", help="Read a persisted storage snapshot")
    snapshot.add_argument("snapshot_id", nargs="?")
    layout = storage_commands.add_parser("layout", help="Inspect exact GPT/MBR layout")
    layout.add_argument("target_disk")
    storage_plan = storage_commands.add_parser("plan", help="Generate declarative partition plan")
    storage_plan.add_argument("operation", choices=("create", "delete", "resize", "move"))
    storage_plan.add_argument("target_disk")
    storage_plan.add_argument("--partition-number", type=int)
    storage_plan.add_argument("--size-bytes", type=int)
    storage_plan.add_argument("--new-size-bytes", type=int)
    storage_plan.add_argument("--new-start-sector", type=int)
    storage_plan.add_argument("--table-type", choices=("GPT", "MBR"))
    validate_storage = storage_commands.add_parser(
        "validate", help="Validate/dry-run/protect operation"
    )
    validate_storage.add_argument("operation_id")
    authorize_storage = storage_commands.add_parser(
        "authorize", help="Request independent authorization"
    )
    authorize_storage.add_argument("operation_id")
    execute_storage = storage_commands.add_parser(
        "execute", help="Execute already-authorized operation"
    )
    execute_storage.add_argument("operation_id")
    status_storage = storage_commands.add_parser("status", help="Read durable storage transaction")
    status_storage.add_argument("operation_id")
    reconcile_storage = storage_commands.add_parser(
        "reconcile", help="Reinspect UNKNOWN transaction"
    )
    reconcile_storage.add_argument("operation_id")

    backup = commands.add_parser("backup", help="Plan, create, list and verify backups")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    plan = backup_commands.add_parser("plan", help="Generate a safe backup plan")
    plan.add_argument("source")
    plan.add_argument("destination")
    create = backup_commands.add_parser("create", help="Plan and request backup.create")
    create.add_argument("source")
    create.add_argument("destination")
    backup_commands.add_parser("list", help="List known backups")
    verify = backup_commands.add_parser("verify", help="Verify one backup")
    verify.add_argument("backup_id")

    filesystem = commands.add_parser("filesystem", help="Inspect and safely repair filesystems")
    filesystem_commands = filesystem.add_subparsers(dest="filesystem_command", required=True)
    inspect = filesystem_commands.add_parser(
        "inspect", help="Inspect one exact device without modifying it"
    )
    inspect.add_argument("device")
    repair = filesystem_commands.add_parser(
        "repair",
        help="Plan, start or inspect filesystem.repair",
    )
    repair.add_argument(
        "repair_action",
        help="'plan', 'status', or an existing repair plan id",
    )
    repair.add_argument(
        "value",
        nargs="?",
        help="device after 'plan', or repair id after 'status'",
    )
    repair.add_argument(
        "--backup-id",
        help="verified full-filesystem backup used to create ProtectionCheckpoint",
    )

    boot = commands.add_parser("boot", help="Diagnose and recover Linux boot state")
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

    consent = commands.add_parser("consent", help="Trusted local approval channel")
    consent_commands = consent.add_subparsers(dest="consent_command", required=True)
    approve = consent_commands.add_parser("approve", help="Inspect and approve one challenge")
    approve.add_argument("challenge_id")
    deny = consent_commands.add_parser("deny", help="Deny one challenge")
    deny.add_argument("challenge_id")
    return parser


def main() -> int:
    return asyncio.run(_main(build_parser().parse_args()))


async def _main(args: argparse.Namespace) -> int:
    if args.command == "consent":
        return await _consent_command(args)

    application = create_app()
    async with application.router.lifespan_context(application):
        if args.command == "storage":
            return await _storage_command(args, application)
        if args.command == "backup":
            return await _backup_command(args, application)
        if args.command == "filesystem":
            return await _filesystem_command(args, application)
        if args.command == "boot":
            return await _boot_command(args, application)
    return 1


async def _storage_command(args: argparse.Namespace, application: FastAPI) -> int:
    service = cast(StorageAnalysisService, application.state.storage_analysis_service)
    if args.storage_command == "analyze":
        try:
            analysis_result = await service.analyze(session_id=f"cli-{uuid4().hex}")
        except StorageAnalysisError as exc:
            print(f"ARES storage analysis failed: {exc.code}", file=sys.stderr)
            return 2
        print(analysis_result.model_dump_json(indent=2))
        return 0
    if args.storage_command == "snapshot":
        snapshot_id = cast(str | None, args.snapshot_id)
        snapshot = (
            await service.snapshot(snapshot_id)
            if snapshot_id is not None
            else await service.latest_snapshot()
        )
        if snapshot is None:
            print("ARES storage snapshot not found", file=sys.stderr)
            return 3
        print(snapshot.model_dump_json(indent=2))
        return 0
    operation_service = cast(StorageOperationService, application.state.storage_operation_service)
    session_id = f"cli-{uuid4().hex}"
    try:
        if args.storage_command == "layout":
            layout_result = await operation_service.layout(cast(str, args.target_disk))
            print(layout_result.model_dump_json(indent=2))
            return 0
        if args.storage_command == "plan":
            payload = StorageOperationPlanRequest(
                operation=cast(Literal["create", "delete", "resize", "move"], args.operation),
                target_disk=cast(str, args.target_disk),
                partition_number=cast(int | None, args.partition_number),
                size_bytes=cast(int | None, args.size_bytes),
                new_size_bytes=cast(int | None, args.new_size_bytes),
                new_start_sector=cast(int | None, args.new_start_sector),
                table_type=(
                    PartitionTableType(cast(str, args.table_type))
                    if args.table_type is not None
                    else None
                ),
            )
            operation_plan = await operation_service.plan(
                payload, session_id=session_id, created_by="local-cli-user"
            )
            print(operation_plan.model_dump_json(indent=2))
            return 0
        operation_id = cast(str, args.operation_id)
        record = await operation_service.get(operation_id)
        if record is not None:
            # CLI commands are separate processes; preserve the transaction's stable session.
            session_id = record.transaction.session_id
        if args.storage_command == "validate":
            validated_plan = await operation_service.validate(operation_id, session_id=session_id)
            print(validated_plan.model_dump_json(indent=2))
            return 0
        if args.storage_command == "authorize":
            accepted = await operation_service.authorize(
                operation_id,
                StorageOperationActionRequest(request_authorization=True),
                session_id=session_id,
            )
            print(accepted.model_dump_json(indent=2))
            print(
                "Poll 'ares storage status <id>'; when a challenge appears, approve it with "
                "'ares consent approve <challenge-id>'.",
                file=sys.stderr,
            )
            return 0
        if args.storage_command == "execute":
            accepted = await operation_service.execute(
                operation_id, session_id=session_id, created_by="local-cli-user"
            )
            print(accepted.model_dump_json(indent=2))
            return 0
        if args.storage_command == "status":
            if record is None:
                print("ARES storage operation not found", file=sys.stderr)
                return 3
            print(record.model_dump_json(indent=2))
            return 0
        if args.storage_command == "reconcile":
            reconciliation = await operation_service.reconcile_unknown(
                operation_id, session_id=session_id
            )
            print(reconciliation.model_dump_json(indent=2))
            return 0
    except StorageOperationServiceError as exc:
        print(f"ARES storage operation failed: {exc.code}", file=sys.stderr)
        return 2
    return 1


async def _backup_command(args: argparse.Namespace, application: FastAPI) -> int:
    service = cast(BackupService, application.state.backup_service)
    session_id = f"cli-{uuid4().hex}"
    try:
        if args.backup_command == "plan":
            plan = await service.plan(
                BackupPlanRequest(source=args.source, destination=args.destination),
                session_id=session_id,
            )
            print(plan.model_dump_json(indent=2))
            return 0
        if args.backup_command == "create":
            plan = await service.plan(
                BackupPlanRequest(source=args.source, destination=args.destination),
                session_id=session_id,
            )
            print("BACKUP PLAN")
            print(plan.model_dump_json(indent=2))
            confirmation = await asyncio.to_thread(
                input,
                "Type REQUEST to request independent authorization for this exact plan: ",
            )
            if confirmation != "REQUEST":
                print("Backup request cancelled before authorization.", file=sys.stderr)
                return 4
            accepted = await service.create(
                BackupCreateRequest(plan_id=plan.id, request_authorization=True),
                session_id=session_id,
                created_by="local-cli-user",
            )
            print(accepted.model_dump_json(indent=2))
            challenge_shown: str | None = None
            while True:
                backup = await service.get(accepted.backup.id)
                if backup is None:
                    print("Backup record disappeared.", file=sys.stderr)
                    return 5
                challenge = backup.execution.authorization_challenge_id
                if challenge is not None and challenge != challenge_shown:
                    challenge_shown = challenge
                    print(
                        "Independent authorization required. In a trusted local terminal run:\n"
                        f"  ares consent approve {challenge}",
                        file=sys.stderr,
                    )
                print(
                    json.dumps(
                        {
                            "backup_id": backup.id,
                            "status": backup.status.value,
                            "progress": backup.execution.progress.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                if backup.status in _TERMINAL_BACKUP_STATES:
                    print(backup.model_dump_json(indent=2))
                    return 0 if backup.status is BackupStatus.COMPLETED else 6
                await asyncio.sleep(0.5)
        if args.backup_command == "list":
            backups = await service.list()
            print(json.dumps([item.model_dump(mode="json") for item in backups], indent=2))
            return 0
        if args.backup_command == "verify":
            result = await service.verify(args.backup_id, session_id=session_id)
            print(result.model_dump_json(indent=2))
            return 0
    except BackupServiceError as exc:
        print(f"ARES backup failed: {exc.code}", file=sys.stderr)
        return 2
    return 1


async def _filesystem_command(args: argparse.Namespace, application: FastAPI) -> int:
    service = cast(FilesystemRepairService, application.state.filesystem_repair_service)
    session_id = f"cli-{uuid4().hex}"
    try:
        if args.filesystem_command == "inspect":
            inspection = await service.inspect(
                FilesystemInspectRequest(device=args.device), session_id=session_id
            )
            print(inspection.model_dump_json(indent=2))
            return 0
        if args.filesystem_command != "repair":
            return 1
        action = cast(str, args.repair_action)
        value = cast(str | None, args.value)
        if action == "plan":
            if value is None:
                print("A device is required after 'repair plan'.", file=sys.stderr)
                return 2
            created_plan = await service.plan(
                FilesystemRepairPlanRequest(device=value, backup_id=args.backup_id),
                session_id=session_id,
            )
            print(created_plan.model_dump_json(indent=2))
            return 0 if created_plan.executable else 3
        if action == "status":
            if value is None:
                print("A repair id is required after 'repair status'.", file=sys.stderr)
                return 2
            record = await service.get(value)
            if record is None:
                print("Filesystem repair not found.", file=sys.stderr)
                return 3
            print(record.model_dump_json(indent=2))
            return 0
        if value is not None or args.backup_id is not None:
            print(
                "Use 'ares filesystem repair <plan-id>' without additional target arguments.",
                file=sys.stderr,
            )
            return 2
        stored_plan = await service.get_plan(action)
        if stored_plan is None:
            print("Filesystem repair plan not found.", file=sys.stderr)
            return 3
        print("FILESYSTEM REPAIR PLAN")
        print(stored_plan.model_dump_json(indent=2))
        if not stored_plan.executable:
            print("Plan is blocked by safety preconditions.", file=sys.stderr)
            return 4
        confirmation = await asyncio.to_thread(
            input,
            "Type REQUEST to request independent authorization for this exact high-risk plan: ",
        )
        if confirmation != "REQUEST":
            print("Repair request cancelled before authorization.", file=sys.stderr)
            return 4
        accepted = await service.start(
            FilesystemRepairStartRequest(plan_id=stored_plan.id, request_authorization=True),
            session_id=session_id,
            created_by="local-cli-user",
        )
        print(accepted.model_dump_json(indent=2))
        challenge_shown: str | None = None
        while True:
            record = await service.get(accepted.id)
            if record is None:
                print("Filesystem repair record disappeared.", file=sys.stderr)
                return 5
            challenge = record.execution.authorization_challenge_id
            if challenge is not None and challenge != challenge_shown:
                challenge_shown = challenge
                print(
                    "Independent high-risk authorization required. Run locally:\n"
                    f"  ares consent approve {challenge}",
                    file=sys.stderr,
                )
            print(
                json.dumps(
                    {
                        "repair_id": record.id,
                        "status": record.execution.status.value,
                        "error_code": record.execution.error_code,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            if record.execution.status in _TERMINAL_REPAIR_STATES:
                print(record.model_dump_json(indent=2))
                return 0 if record.execution.status is RepairExecutionStatus.COMPLETED else 6
            await asyncio.sleep(0.5)
    except FilesystemServiceError as exc:
        print(f"ARES filesystem operation failed: {exc.code}", file=sys.stderr)
        return 2


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
            repair_plan = await service.plan(
                BootRepairPlanRequest(
                    diagnostic_id=cast(str, args.diagnostic_id),
                    target_os_id=cast(str | None, args.target_os_id),
                ),
                session_id=session_id,
            )
            print(repair_plan.model_dump_json(indent=2))
            return 0 if repair_plan.executable else 3
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
                "Type REQUEST to create the boot checkpoint and request "
                "independent authorization: ",
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
            reconciliation = await service.reconcile(
                repair_id, session_id=record.execution.session_id
            )
            print(reconciliation.model_dump_json(indent=2))
            return 0
    except BootRecoveryServiceError as exc:
        print(f"ARES boot recovery failed: {exc.code}", file=sys.stderr)
        return 2
    return 1


async def _consent_command(args: argparse.Namespace) -> int:
    client = UnixConsentOperatorClient(get_settings().consent_socket)
    try:
        challenge = await client.get(args.challenge_id)
        print(json.dumps(challenge, indent=2, ensure_ascii=False))
        if args.consent_command == "deny":
            await client.deny(args.challenge_id)
            print("Authorization denied.")
            return 0
        expected = challenge.get("confirmation_phrase")
        if not isinstance(expected, str):
            print("Consent challenge is invalid.", file=sys.stderr)
            return 2
        confirmation = await asyncio.to_thread(input, f"Type exactly '{expected}': ")
        if confirmation != expected:
            print("Exact confirmation did not match; nothing was authorized.", file=sys.stderr)
            return 4
        await client.approve(args.challenge_id, confirmation)
        print("One-use authorization granted for the exact displayed plan.")
        return 0
    except RuntimeError as exc:
        print(f"ARES consent failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
