"""ARES command-line interface backed by the same application services as the API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import cast
from uuid import uuid4

from ares.backup import BackupCreateRequest, BackupPlanRequest, BackupService, BackupServiceError
from ares.backup.models import BackupStatus
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

_TERMINAL_BACKUP_STATES = {
    BackupStatus.COMPLETED,
    BackupStatus.FAILED,
    BackupStatus.CANCELLED,
    BackupStatus.CORRUPTED,
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
    return 1


async def _storage_command(args: argparse.Namespace, application) -> int:
    service = cast(StorageAnalysisService, application.state.storage_analysis_service)
    if args.storage_command == "analyze":
        try:
            result = await service.analyze(session_id=f"cli-{uuid4().hex}")
        except StorageAnalysisError as exc:
            print(f"ARES storage analysis failed: {exc.code}", file=sys.stderr)
            return 2
        print(result.model_dump_json(indent=2))
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
    return 1


async def _backup_command(args: argparse.Namespace, application) -> int:
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


async def _filesystem_command(args: argparse.Namespace, application) -> int:
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
            plan = await service.plan(
                FilesystemRepairPlanRequest(device=value, backup_id=args.backup_id),
                session_id=session_id,
            )
            print(plan.model_dump_json(indent=2))
            return 0 if plan.executable else 3
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
        plan = await service.get_plan(action)
        if plan is None:
            print("Filesystem repair plan not found.", file=sys.stderr)
            return 3
        print("FILESYSTEM REPAIR PLAN")
        print(plan.model_dump_json(indent=2))
        if not plan.executable:
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
            FilesystemRepairStartRequest(plan_id=plan.id, request_authorization=True),
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
