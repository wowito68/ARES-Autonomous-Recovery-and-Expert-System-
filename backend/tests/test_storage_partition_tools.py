from __future__ import annotations

import json

import pytest

from ares.storage_operations.models import PartitionRole, PartitionTableType, StorageDeviceIdentity
from ares.tools.partition import (
    PartitionToolError,
    build_empty_table,
    new_partition_resource,
    parse_blkid_export,
    parse_sfdisk_json,
    render_sfdisk_script,
    with_partitions,
)


def _identity(path: str = "/fixture.img") -> StorageDeviceIdentity:
    return StorageDeviceIdentity(
        requested_path=path,
        canonical_path=path,
        device_kind="regular_file",
        major_minor="file:1:1",
        size_bytes=256 * 1024 * 1024,
        logical_sector_size=512,
        physical_sector_size=512,
        controlled_test_target=True,
        fingerprint_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    ("label", "expected"), [("gpt", PartitionTableType.GPT), ("dos", PartitionTableType.MBR)]
)
def test_parse_sfdisk_supports_gpt_and_mbr(label: str, expected: PartitionTableType) -> None:
    identity = _identity()
    payload = {
        "partitiontable": {
            "label": label,
            "id": "fixture",
            "device": identity.canonical_path,
            "unit": "sectors",
            "sectorsize": 512,
            "firstlba": 2048,
            "lastlba": 524200,
            "partitions": [
                {
                    "node": f"{identity.canonical_path}1",
                    "start": 2048,
                    "size": 4096,
                    "type": ("U" if label == "dos" else "0FC63DAF-8483-4772-8E79-3D69D8477DE4"),
                }
            ],
        }
    }
    table = parse_sfdisk_json(json.dumps(payload), identity)
    assert table.type is expected
    assert table.partitions[0].number == 1
    assert table.free_regions
    assert len(table.fingerprint_sha256) == 64


def test_render_script_is_exact_and_never_embeds_shell_or_force_flags() -> None:
    identity = _identity()
    table = build_empty_table(
        identity, PartitionTableType.GPT, "11111111-2222-3333-4444-555555555555"
    )
    partition = new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.GPT,
        number=1,
        start_sector=2048,
        size_sectors=4096,
        sector_size=512,
        type_code=None,
        name="ARES TEST",
    )
    script = render_sfdisk_script(with_partitions(table, (partition,)), identity)
    assert "label: gpt" in script
    assert "start=2048" in script
    assert "size=4096" in script
    assert "--force" not in script
    assert "--move-data" not in script
    assert ";" not in script


def test_efi_and_blkid_crypto_lvm_raid_detection_inputs_are_structured() -> None:
    identity = _identity()
    payload = {
        "partitiontable": {
            "label": "gpt",
            "sectorsize": 512,
            "firstlba": 34,
            "lastlba": 524200,
            "partitions": [
                {
                    "node": "/fixture.img1",
                    "start": 2048,
                    "size": 4096,
                    "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                }
            ],
        }
    }
    table = parse_sfdisk_json(json.dumps(payload), identity)
    assert table.partitions[0].role is PartitionRole.EFI
    assert parse_blkid_export("TYPE=crypto_LUKS\nUSAGE=crypto\n")["TYPE"] == "crypto_LUKS"
    assert parse_blkid_export("TYPE=LVM2_member\nUSAGE=other\n")["TYPE"] == "LVM2_member"
    assert parse_blkid_export("TYPE=linux_raid_member\nUSAGE=raid\n")["USAGE"] == "raid"


def test_unsafe_partition_name_is_rejected() -> None:
    identity = _identity()
    with pytest.raises(PartitionToolError, match="STORAGE_PARTITION_NAME_UNSAFE"):
        new_partition_resource(
            identity=identity,
            table_type=PartitionTableType.GPT,
            number=1,
            start_sector=2048,
            size_sectors=4096,
            sector_size=512,
            type_code=None,
            name='bad"\nname',
        )
