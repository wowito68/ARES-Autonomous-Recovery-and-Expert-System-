"""ARES command-line interface backed by the same application services as the API."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import cast
from uuid import uuid4

from ares.main import create_app
from ares.storage.service import StorageAnalysisError, StorageAnalysisService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ares")
    commands = parser.add_subparsers(dest="command", required=True)
    storage = commands.add_parser("storage", help="Read-only storage diagnostics")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("analyze", help="Run storage.disk-analysis")
    snapshot = storage_commands.add_parser("snapshot", help="Read a persisted storage snapshot")
    snapshot.add_argument("snapshot_id", nargs="?")
    return parser


def main() -> int:
    return asyncio.run(_main(build_parser().parse_args()))


async def _main(args: argparse.Namespace) -> int:
    application = create_app()
    async with application.router.lifespan_context(application):
        service = cast(StorageAnalysisService, application.state.storage_analysis_service)
        if args.command == "storage" and args.storage_command == "analyze":
            try:
                result = await service.analyze(session_id=f"cli-{uuid4().hex}")
            except StorageAnalysisError as exc:
                print(f"ARES storage analysis failed: {exc.code}", file=sys.stderr)
                return 2
            print(result.model_dump_json(indent=2))
            return 0
        if args.command == "storage" and args.storage_command == "snapshot":
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


if __name__ == "__main__":
    raise SystemExit(main())
