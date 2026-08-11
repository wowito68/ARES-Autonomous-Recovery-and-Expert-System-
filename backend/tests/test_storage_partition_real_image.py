from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ares.protection import ProtectionCheckpointStore
from ares.storage_operations import (
    DeclarativeStorageOperationRequest,
    LocalTestStorageExecutor,
    PartitionTableType,
    ProductionStorageWriteGate,
    StorageOperationEngine,
    StorageOperationStore,
    StorageTransactionStatus,
    StorageVerification,
)
from ares.tools.partition import StoragePartitionToolSuite

pytestmark = pytest.mark.skipif(shutil.which("sfdisk") is None, reason="sfdisk unavailable")


async def _engine(tmp_path: Path) -> StorageOperationEngine:
    operation_store = StorageOperationStore(tmp_path / "operations")
    checkpoint_store = ProtectionCheckpointStore(tmp_path / "checkpoints")
    operation_store.prepare()
    checkpoint_store.prepare()
    gate = ProductionStorageWriteGate(test_mode=True)
    tools = StoragePartitionToolSuite(
        allow_regular_file_targets=True,
        write_gate=gate,
    )
    return StorageOperationEngine(
        executor=LocalTestStorageExecutor(tools),
        store=operation_store,
        checkpoints=checkpoint_store,
        write_gate=gate,
    )


async def _authorize_and_execute(
    engine: StorageOperationEngine, operation_id: str, session_id: str
) -> StorageVerification:
    challenges: list[str] = []

    async def challenge(value: str) -> None:
        challenges.append(value)

    grant = await engine.request_authorization(
        operation_id, session_id=session_id, on_challenge=challenge
    )

    async def stage(name: str, payload: dict[str, object]) -> None:
        del name, payload

    verification = await engine.execute(operation_id, grant, session_id=session_id, on_stage=stage)
    assert challenges
    return verification


async def test_create_and_delete_partition_on_regular_disk_image(tmp_path: Path) -> None:
    image = tmp_path / "test-disk.img"
    with image.open("wb") as handle:
        handle.truncate(128 * 1024 * 1024)
    engine = await _engine(tmp_path)
    session = "storage-image-test-session"

    create = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk=str(image),
            size_bytes=16 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
            partition_name="ARES TEST",
        ),
        session_id=session,
        created_by="pytest",
    )
    assert create.executable is False
    validated = await engine.validate(create.operation_id, session_id=session)
    assert validated.dry_run is not None and validated.dry_run.valid is True
    assert validated.protection_checkpoint is not None
    assert validated.executable is True
    verification = await _authorize_and_execute(engine, create.operation_id, session)
    assert verification.status.value == "VERIFIED"
    created_layout = await engine.inspect(str(image))
    assert created_layout.partition_table.type is PartitionTableType.GPT
    assert len(created_layout.partition_table.partitions) == 1

    delete = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="delete",
            target_disk=str(image),
            partition_number=1,
        ),
        session_id=session,
        created_by="pytest",
    )
    validated_delete = await engine.validate(delete.operation_id, session_id=session)
    assert validated_delete.executable is True
    deleted = await _authorize_and_execute(engine, delete.operation_id, session)
    assert deleted.status.value == "VERIFIED"
    final_layout = await engine.inspect(str(image))
    assert final_layout.partition_table.partitions == ()
    record = await engine.store.get_record(delete.operation_id)
    assert record is not None
    assert record.transaction.status is StorageTransactionStatus.COMMITTED
