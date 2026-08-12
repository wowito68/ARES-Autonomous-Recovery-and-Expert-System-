from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from ares.protection import (
    ProtectionCheckpoint,
    ProtectionCheckpointStatus,
    ProtectionCheckpointStore,
)
from ares.storage_operations.engine import (
    DeclarativeStorageOperationRequest,
    StorageOperationEngine,
    StorageOperationEngineError,
    _align_up,
    _filesystem_impact,
    _next_partition_number,
    _overlaps,
    _required_operations,
)
from ares.storage_operations.executor import StorageExecutorError
from ares.storage_operations.integrity import (
    canonical_sha256,
    device_identity_fingerprint,
    layout_fingerprint,
)
from ares.storage_operations.models import (
    DataImpactAssessment,
    DataImpactLevel,
    FreeRegion,
    PartitionResource,
    PartitionTable,
    PartitionTableCheckpointArtifact,
    PartitionTableType,
    PhysicalDisk,
    StorageAuthorizationGrant,
    StorageCheckpointBundle,
    StorageDeviceIdentity,
    StorageDryRunResult,
    StorageLayout,
    StorageOperationOutcome,
    StorageOperationPlan,
    StorageTransactionStatus,
    StorageVerificationStatus,
)
from ares.storage_operations.policy import ProductionStorageWriteGate
from ares.storage_operations.store import StorageOperationStore, _safe_id
from ares.tools.partition import build_empty_table, new_partition_resource, with_partitions


class FakeExecutor:
    def __init__(self, layout: StorageLayout) -> None:
        self.current = layout
        self.dry_run_valid = True
        self.error_on: dict[str, str] = {}
        self.bad_checkpoint = False
        self.challenge_ids: list[str] = []
        self.stages: list[str] = []

    def _error(self, name: str) -> None:
        code = self.error_on.get(name)
        if code is not None:
            raise StorageExecutorError(code)

    async def inspect(self, target_disk: str) -> StorageLayout:
        del target_disk
        self._error("inspect")
        return self.current

    async def dry_run(self, plan: StorageOperationPlan) -> StorageDryRunResult:
        self._error("dry_run")
        return StorageDryRunResult(
            original_layout_sha256=layout_fingerprint(plan.original_layout),
            proposed_layout_sha256=layout_fingerprint(plan.proposed_layout),
            script_sha256="d" * 64,
            exit_code=0 if self.dry_run_valid else 1,
            valid=self.dry_run_valid,
            warnings=() if self.dry_run_valid else ("fixture rejection",),
        )

    async def create_checkpoint(self, plan: StorageOperationPlan) -> StorageCheckpointBundle:
        self._error("checkpoint")
        checkpoint_id = uuid4().hex
        artifact = PartitionTableCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            operation_id=plan.operation_id,
            target_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            partition_table_fingerprint_sha256=(
                "e" * 64
                if self.bad_checkpoint
                else plan.original_layout.partition_table.fingerprint_sha256
            ),
            dump_sha256="f" * 64,
            sfdisk_dump="label: gpt\n",
        )
        checkpoint = ProtectionCheckpoint(
            id=checkpoint_id,
            status=ProtectionCheckpointStatus.READY,
            protected_resources=(plan.protected_resource_id,),
            resource_fingerprints={plan.protected_resource_id: plan.target_disk.fingerprint_sha256},
            provider_capability_id="storage.partition.inspect",
            protection_kind="snapshot",
            verification_id=f"partition-table:{artifact.dump_sha256[:24]}",
            session_id=plan.session_id,
            evidence_sha256=canonical_sha256({"checkpoint_id": checkpoint_id}),
        )
        return StorageCheckpointBundle(checkpoint=checkpoint, artifact=artifact)

    async def request_authorization(
        self,
        plan: StorageOperationPlan,
        *,
        on_challenge: object,
    ) -> StorageAuthorizationGrant:
        self._error("authorize")
        challenge_id = "challenge-engine-1234"
        callback = on_challenge
        assert callable(callback)
        await callback(challenge_id)
        self.challenge_ids.append(challenge_id)
        return StorageAuthorizationGrant(
            id="grant-engine-1234",
            challenge_id=challenge_id,
            operation_id=plan.operation_id,
            plan_id=plan.id,
            session_id=plan.session_id,
            target_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
            plan_fingerprint_sha256=plan.fingerprint_sha256,
            operator_uid=1000,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def execute(
        self,
        plan: StorageOperationPlan,
        grant: StorageAuthorizationGrant,
        *,
        on_stage: object,
    ) -> StorageOperationOutcome:
        del grant
        self._error("execute")
        callback = on_stage
        assert callable(callback)
        await callback("storage.partition-table.updated", {"operation": plan.operation.value})
        self.stages.append("storage.partition-table.updated")
        before = self.current
        self.current = plan.proposed_layout
        return StorageOperationOutcome(
            operation_id=plan.operation_id,
            operation=plan.operation,
            tool="sfdisk",
            exit_code=0,
            before=before,
            after=self.current,
            evidence=("fake-executor",),
        )

    async def verify(self, plan: StorageOperationPlan) -> StorageLayout:
        del plan
        self._error("verify")
        return self.current


def _identity(
    path: str = "/fixture.img",
    *,
    kind: Literal["regular_file", "loop", "block"] = "regular_file",
    controlled: bool = True,
    fingerprint_char: str = "a",
) -> StorageDeviceIdentity:
    draft = StorageDeviceIdentity(
        requested_path=path,
        canonical_path=path,
        device_kind=kind,
        major_minor="file:1:1" if kind == "regular_file" else "8:0",
        model=f"fixture-{fingerprint_char}",
        size_bytes=64 * 1024 * 1024,
        logical_sector_size=512,
        physical_sector_size=512,
        controlled_test_target=controlled,
        fingerprint_sha256=fingerprint_char * 64,
    )
    return draft.model_copy(update={"fingerprint_sha256": device_identity_fingerprint(draft)})


def _layout(
    *,
    identity: StorageDeviceIdentity | None = None,
    table_type: PartitionTableType = PartitionTableType.GPT,
    partitions: tuple[PartitionResource, ...] = (),
    free_regions: tuple[FreeRegion, ...] | None = None,
) -> StorageLayout:
    disk_identity = identity or _identity()
    if table_type is PartitionTableType.UNKNOWN:
        table = PartitionTable(
            type=PartitionTableType.UNKNOWN,
            sector_size=512,
            first_usable_sector=2048,
            last_usable_sector=131071,
            total_sectors=131072,
            partitions=partitions,
            free_regions=(
                free_regions
                if free_regions is not None
                else (
                    FreeRegion(
                        start_sector=2048,
                        end_sector=131071,
                        size_sectors=129024,
                        size_bytes=129024 * 512,
                    ),
                )
            ),
            fingerprint_sha256="b" * 64,
        )
    else:
        table = with_partitions(
            build_empty_table(disk_identity, table_type, "fixture-guid"),
            partitions,
        )
        if free_regions is not None:
            table = table.model_copy(update={"free_regions": free_regions})
    disk = PhysicalDisk(
        id=disk_identity.resource_id,
        identity=disk_identity,
        partition_table_id=f"partition-table:{disk_identity.fingerprint_sha256[:24]}",
    )
    return StorageLayout(
        disk=disk,
        partition_table=table,
        evidence_sha256=canonical_sha256(
            {
                "disk": disk.model_dump(mode="json"),
                "table": table.model_dump(mode="json"),
            }
        ),
    )


def _partition(
    identity: StorageDeviceIdentity,
    number: int,
    start: int,
    size: int,
) -> PartitionResource:
    return new_partition_resource(
        identity=identity,
        table_type=PartitionTableType.GPT,
        number=number,
        start_sector=start,
        size_sectors=size,
        sector_size=512,
        type_code=None,
        name=f"P{number}",
    )


def _engine(
    tmp_path: Path,
    layout: StorageLayout,
    *,
    test_mode: bool = True,
) -> tuple[StorageOperationEngine, FakeExecutor, StorageOperationStore, ProtectionCheckpointStore]:
    store = StorageOperationStore(tmp_path / "operations")
    store.prepare()
    checkpoints = ProtectionCheckpointStore(tmp_path / "checkpoints")
    checkpoints.prepare()
    executor = FakeExecutor(layout)
    engine = StorageOperationEngine(
        executor=executor,
        store=store,
        checkpoints=checkpoints,
        write_gate=ProductionStorageWriteGate(test_mode=test_mode),
    )
    return engine, executor, store, checkpoints


async def _validated_create(
    tmp_path: Path,
) -> tuple[StorageOperationEngine, FakeExecutor, StorageOperationStore, StorageOperationPlan]:
    engine, executor, store, _ = _engine(tmp_path, _layout(table_type=PartitionTableType.UNKNOWN))
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk="/fixture.img",
            size_bytes=8 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="engine-session-1234",
        created_by="test",
    )
    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    assert validated.executable is True
    return engine, executor, store, validated


async def test_plan_rejects_table_mismatch_missing_partition_free_region_and_space(
    tmp_path: Path,
) -> None:
    engine, executor, _, _ = _engine(tmp_path / "a", _layout())
    with pytest.raises(StorageOperationEngineError, match="STORAGE_PARTITION_TABLE_TYPE_MISMATCH"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="create",
                target_disk="/fixture.img",
                size_bytes=4 * 1024 * 1024,
                table_type=PartitionTableType.MBR,
            ),
            session_id="session-plan-1234",
            created_by="test",
        )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_FREE_REGION_NOT_FOUND"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="create",
                target_disk="/fixture.img",
                size_bytes=4 * 1024 * 1024,
                free_region_index=100,
            ),
            session_id="session-plan-1234",
            created_by="test",
        )

    tiny = _layout(
        free_regions=(
            FreeRegion(
                start_sector=2048, end_sector=4095, size_sectors=2048, size_bytes=1024 * 1024
            ),
        )
    )
    executor.current = tiny
    with pytest.raises(StorageOperationEngineError, match="STORAGE_INSUFFICIENT_FREE_SPACE"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="create", target_disk="/fixture.img", size_bytes=8 * 1024 * 1024
            ),
            session_id="session-plan-1234",
            created_by="test",
        )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_PARTITION_NOT_FOUND"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="delete", target_disk="/fixture.img", partition_number=9
            ),
            session_id="session-plan-1234",
            created_by="test",
        )


async def test_resize_and_move_planning_validation_edges(tmp_path: Path) -> None:
    identity = _identity()
    p1 = _partition(identity, 1, 2048, 4096)
    p2 = _partition(identity, 2, 8192, 4096)
    engine, executor, _, _ = _engine(tmp_path, _layout(identity=identity, partitions=(p1, p2)))

    with pytest.raises(StorageOperationEngineError, match="STORAGE_RESIZE_SIZE_INVALID"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="resize",
                target_disk="/fixture.img",
                partition_number=1,
                new_size_bytes=p1.size_bytes,
            ),
            session_id="resize-session-1234",
            created_by="test",
        )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_PARTITION_CONFLICT"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="resize",
                target_disk="/fixture.img",
                partition_number=1,
                new_size_bytes=8 * 1024 * 1024,
            ),
            session_id="resize-session-1234",
            created_by="test",
        )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_MOVE_BOUNDARY_INVALID"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="move",
                target_disk="/fixture.img",
                partition_number=1,
                new_start_sector=1,
            ),
            session_id="move-session-1234",
            created_by="test",
        )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_PARTITION_CONFLICT"):
        await engine.plan(
            DeclarativeStorageOperationRequest(
                operation="move",
                target_disk="/fixture.img",
                partition_number=1,
                new_start_sector=p2.start_sector,
            ),
            session_id="move-session-1234",
            created_by="test",
        )

    executor.current = _layout(identity=identity, partitions=(p1,))
    resize = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="resize",
            target_disk="/fixture.img",
            partition_number=1,
            new_size_bytes=3 * 1024 * 1024,
        ),
        session_id="resize-session-1234",
        created_by="test",
    )
    validated = await engine.validate(resize.operation_id, session_id=resize.session_id)
    assert validated.executable is False
    assert "operation_adapter_disabled" in validated.limitations


async def test_validate_rejects_not_found_session_status_expiry_identity_and_layout(
    tmp_path: Path,
) -> None:
    engine, executor, store, _ = _engine(tmp_path, _layout(table_type=PartitionTableType.UNKNOWN))
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_FOUND"):
        await engine.validate("missing-operation", session_id="session-valid-1234")

    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk="/fixture.img",
            size_bytes=4 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="session-valid-1234",
        created_by="test",
    )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_SESSION_MISMATCH"):
        await engine.validate(plan.operation_id, session_id="different-session")

    transaction = await store.get_transaction(plan.operation_id)
    assert transaction is not None
    await store.put_transaction(
        transaction.model_copy(update={"status": StorageTransactionStatus.FAILED})
    )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_VALIDATABLE"):
        await engine.validate(plan.operation_id, session_id=plan.session_id)

    await store.put_transaction(transaction)
    expired = plan.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    await store.put_plan(expired)
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_PLAN_EXPIRED"):
        await engine.validate(plan.operation_id, session_id=plan.session_id)

    await store.put_plan(plan)
    executor.current = _layout(
        identity=_identity(fingerprint_char="c"), table_type=PartitionTableType.UNKNOWN
    )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_DEVICE_IDENTITY_CHANGED"):
        await engine.validate(plan.operation_id, session_id=plan.session_id)

    executor.current = _layout(table_type=PartitionTableType.GPT)
    with pytest.raises(StorageOperationEngineError, match="STORAGE_LAYOUT_CHANGED"):
        await engine.validate(plan.operation_id, session_id=plan.session_id)


async def test_validate_dry_run_checkpoint_and_executor_failures(tmp_path: Path) -> None:
    engine, executor, _, _ = _engine(
        tmp_path / "dry", _layout(table_type=PartitionTableType.UNKNOWN)
    )
    plan = await engine.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk="/fixture.img",
            size_bytes=4 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="session-dryrun-1234",
        created_by="test",
    )
    executor.dry_run_valid = False
    validated = await engine.validate(plan.operation_id, session_id=plan.session_id)
    assert validated.executable is False
    assert "dry_run_rejected_proposed_layout" in validated.limitations

    engine2, executor2, _, _ = _engine(
        tmp_path / "checkpoint", _layout(table_type=PartitionTableType.UNKNOWN)
    )
    plan2 = await engine2.plan(
        DeclarativeStorageOperationRequest(
            operation="create",
            target_disk="/fixture.img",
            size_bytes=4 * 1024 * 1024,
            table_type=PartitionTableType.GPT,
        ),
        session_id="session-checkpoint-1234",
        created_by="test",
    )
    executor2.bad_checkpoint = True
    with pytest.raises(StorageOperationEngineError, match="STORAGE_PROTECTION_CHECKPOINT_INVALID"):
        await engine2.validate(plan2.operation_id, session_id=plan2.session_id)

    engine3, executor3, _, _ = _engine(tmp_path / "error", _layout())
    executor3.error_on["inspect"] = "STORAGE_INSPECTION_FAILED"
    with pytest.raises(StorageOperationEngineError, match="STORAGE_INSPECTION_FAILED"):
        await engine3.inspect("/fixture.img")


async def test_authorization_preconditions_and_executor_error(tmp_path: Path) -> None:
    engine, executor, store, plan = await _validated_create(tmp_path)
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_FOUND"):
        await engine.request_authorization(
            "missing-operation",
            session_id=plan.session_id,
            on_challenge=lambda _: asyncio.sleep(0),
        )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_SESSION_MISMATCH"):
        await engine.request_authorization(
            plan.operation_id,
            session_id="different-session",
            on_challenge=lambda _: asyncio.sleep(0),
        )

    executor.error_on["authorize"] = "STORAGE_AUTHORIZATION_DENIED"
    with pytest.raises(StorageOperationEngineError, match="STORAGE_AUTHORIZATION_DENIED"):
        await engine.request_authorization(
            plan.operation_id,
            session_id=plan.session_id,
            on_challenge=lambda _: asyncio.sleep(0),
        )
    executor.error_on.clear()

    record = await store.get_record(plan.operation_id)
    assert record is not None
    await store.put_transaction(
        record.transaction.model_copy(update={"status": StorageTransactionStatus.VALIDATED})
    )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_AUTHORIZABLE"):
        await engine.request_authorization(
            plan.operation_id,
            session_id=plan.session_id,
            on_challenge=lambda _: asyncio.sleep(0),
        )


async def test_execute_maps_failures_to_failed_or_unknown_and_checks_preconditions(
    tmp_path: Path,
) -> None:
    engine, executor, store, plan = await _validated_create(tmp_path / "unknown")
    grant = await engine.request_authorization(
        plan.operation_id,
        session_id=plan.session_id,
        on_challenge=lambda _: asyncio.sleep(0),
    )
    executor.error_on["execute"] = "STORAGE_BROKER_DISCONNECTED"
    with pytest.raises(StorageOperationEngineError, match="STORAGE_BROKER_DISCONNECTED"):
        await engine.execute(
            plan.operation_id,
            grant,
            session_id=plan.session_id,
            on_stage=lambda _name, _payload: asyncio.sleep(0),
        )
    record = await store.get_record(plan.operation_id)
    assert record is not None and record.transaction.status is StorageTransactionStatus.UNKNOWN

    engine2, executor2, store2, plan2 = await _validated_create(tmp_path / "failed")
    grant2 = await engine2.request_authorization(
        plan2.operation_id,
        session_id=plan2.session_id,
        on_challenge=lambda _: asyncio.sleep(0),
    )
    executor2.error_on["execute"] = "STORAGE_WRITE_FAILED"
    with pytest.raises(StorageOperationEngineError, match="STORAGE_WRITE_FAILED"):
        await engine2.execute(
            plan2.operation_id,
            grant2,
            session_id=plan2.session_id,
            on_stage=lambda _name, _payload: asyncio.sleep(0),
        )
    record2 = await store2.get_record(plan2.operation_id)
    assert record2 is not None and record2.transaction.status is StorageTransactionStatus.FAILED

    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_AUTHORIZED"):
        await engine2.execute_authorized(
            plan2.operation_id,
            session_id=plan2.session_id,
            on_stage=lambda _name, _payload: asyncio.sleep(0),
        )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_NOT_FOUND"):
        await engine2.execute(
            "missing-operation",
            grant2,
            session_id=plan2.session_id,
            on_stage=lambda _name, _payload: asyncio.sleep(0),
        )


async def test_verify_outcome_and_reconcile_unexpected_and_non_unknown_states(
    tmp_path: Path,
) -> None:
    engine, executor, store, plan = await _validated_create(tmp_path)
    bad_identity = plan.proposed_layout.model_copy(
        update={
            "disk": plan.proposed_layout.disk.model_copy(
                update={"identity": _identity(fingerprint_char="c")}
            )
        }
    )
    outcome = StorageOperationOutcome(
        operation_id=plan.operation_id,
        operation=plan.operation,
        tool="sfdisk",
        exit_code=0,
        before=plan.original_layout,
        after=bad_identity,
    )
    verification = engine.verify_outcome(plan, outcome)
    assert verification.status is StorageVerificationStatus.FAILED
    assert verification.identity_verified is False

    record = await store.get_record(plan.operation_id)
    assert record is not None
    with pytest.raises(StorageOperationEngineError, match="STORAGE_TRANSACTION_NOT_UNKNOWN"):
        await engine.reconcile_unknown(plan.operation_id, session_id=plan.session_id)

    unexpected_table = plan.proposed_layout.partition_table.model_copy(
        update={
            "partitions": (),
            "fingerprint_sha256": "9" * 64,
        }
    )
    executor.current = plan.proposed_layout.model_copy(
        update={"partition_table": unexpected_table, "evidence_sha256": "8" * 64}
    )
    await store.put_transaction(
        record.transaction.model_copy(
            update={"status": StorageTransactionStatus.UNKNOWN, "reconciliation_required": True}
        )
    )
    unknown = await engine.reconcile_unknown(plan.operation_id, session_id=plan.session_id)
    assert unknown.status is StorageVerificationStatus.UNKNOWN
    refreshed = await store.get_record(plan.operation_id)
    assert (
        refreshed is not None and refreshed.transaction.status is StorageTransactionStatus.UNKNOWN
    )

    with pytest.raises(StorageOperationEngineError, match="STORAGE_OPERATION_SESSION_MISMATCH"):
        await engine.reconcile_unknown(plan.operation_id, session_id="different-session")


async def test_mark_status_helpers_and_store_invalid_records(tmp_path: Path) -> None:
    engine, _, store, plan = await _validated_create(tmp_path / "state")
    await engine.mark_failed(plan.operation_id, "FAILED_FIXTURE")
    record = await store.get_record(plan.operation_id)
    assert record is not None
    assert record.transaction.status is StorageTransactionStatus.FAILED
    assert record.transaction.finished_at is not None

    await engine.mark_aborted(plan.operation_id, "ABORT_FIXTURE")
    record = await store.get_record(plan.operation_id)
    assert record is not None and record.transaction.status is StorageTransactionStatus.ABORTED
    await engine.mark_unknown("missing-operation", "NOOP")
    await engine.mark_failed("missing-operation", "NOOP")
    await engine.mark_aborted("missing-operation", "NOOP")

    assert _safe_id("bad/id") == "invalid"
    assert _safe_id("short") == "invalid"
    assert _safe_id("valid-id-123") == "valid-id-123"
    assert await store.get_plan("bad/id") is None

    invalid = store.transactions / "invalid-record.json"
    invalid.write_text("[]", encoding="utf-8")
    assert StorageOperationStore._read(invalid) is None
    invalid.write_text("not-json", encoding="utf-8")
    assert StorageOperationStore._read(invalid) is None
    symlink = store.transactions / "symlink-record.json"
    symlink.symlink_to(invalid)
    assert StorageOperationStore._read(symlink) is None

    records = await store.list_records()
    assert any(item.plan.operation_id == plan.operation_id for item in records)


def test_storage_engine_private_helpers_cover_limits_and_impact() -> None:
    assert _align_up(2049, 2048) == 4096
    assert _overlaps(1, 5, 5, 10) is True
    assert _overlaps(1, 4, 5, 10) is False
    assert len(_required_operations("create", None)) == 3
    assert _required_operations("resize", 1)[0].enabled is False
    assert _required_operations("move", 1)[0].kind == "move_partition"

    assert (
        _filesystem_impact(
            DataImpactAssessment(
                level=DataImpactLevel.HIGH,
                filesystem_change_required=True,
                executable=False,
            )
        )
        == "filesystem_change_required_and_not_supported"
    )
    assert (
        _filesystem_impact(DataImpactAssessment(level=DataImpactLevel.UNKNOWN, executable=False))
        == "filesystem_or_data_impact_blocks_execution"
    )
    assert (
        _filesystem_impact(DataImpactAssessment(level=DataImpactLevel.LOW, executable=True))
        == "no_filesystem_content_change_planned"
    )

    identity = _identity()
    gpt = tuple(
        _partition(identity, number, 2048 + number * 4096, 1024) for number in range(1, 129)
    )
    with pytest.raises(StorageOperationEngineError, match="STORAGE_GPT_PARTITION_LIMIT"):
        _next_partition_number(PartitionTableType.GPT, gpt)
    mbr = gpt[:4]
    with pytest.raises(StorageOperationEngineError, match="STORAGE_MBR_PRIMARY_LIMIT"):
        _next_partition_number(PartitionTableType.MBR, mbr)
    assert _next_partition_number(PartitionTableType.GPT, (gpt[0], gpt[2])) == 2
