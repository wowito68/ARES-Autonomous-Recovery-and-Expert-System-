from __future__ import annotations

from ares.cli import build_parser


def test_filesystem_cli_parses_inspect_plan_execute_and_status() -> None:
    parser = build_parser()

    inspect = parser.parse_args(["filesystem", "inspect", "/dev/nvme0n1p3"])
    assert inspect.command == "filesystem"
    assert inspect.filesystem_command == "inspect"
    assert inspect.device == "/dev/nvme0n1p3"

    plan = parser.parse_args(
        [
            "filesystem",
            "repair",
            "plan",
            "/dev/nvme0n1p3",
            "--backup-id",
            "backup-12345678",
        ]
    )
    assert plan.filesystem_command == "repair"
    assert plan.repair_action == "plan"
    assert plan.value == "/dev/nvme0n1p3"
    assert plan.backup_id == "backup-12345678"

    execute = parser.parse_args(["filesystem", "repair", "plan-12345678"])
    assert execute.repair_action == "plan-12345678"
    assert execute.value is None
    assert execute.backup_id is None

    status = parser.parse_args(["filesystem", "repair", "status", "repair-12345678"])
    assert status.repair_action == "status"
    assert status.value == "repair-12345678"
