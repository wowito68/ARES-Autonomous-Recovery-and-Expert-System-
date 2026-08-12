from __future__ import annotations

from typing import Never

import pytest
from fastapi import FastAPI, Request

from ares.actions.base import ActionError
from ares.actions.storage_operations import ValidateAuthorizedStorageOperationAction
from ares.api.routes import storage_operations as routes
from ares.capabilities.plugins.storage_partition import _StorageVerificationPostcheck
from ares.core.problems import AresProblem
from ares.storage_operations.models import (
    PartitionTableType,
    StorageOperationExecutionInput,
    StorageTransactionStatus,
)
from ares.storage_operations.service import (
    StorageOperationPlanRequest,
    StorageOperationServiceError,
)
from tests.test_storage_final_coverage import _context
from tests.test_storage_operation_runtime import _validated_create


class _FailingStorageService:
    async def layout(self, target_disk: str) -> Never:
        del target_disk
        raise StorageOperationServiceError("STORAGE_LAYOUT_FIXTURE_FAILED")

    async def plan(
        self,
        payload: StorageOperationPlanRequest,
        *,
        session_id: str,
        created_by: str,
    ) -> Never:
        del payload, session_id, created_by
        raise StorageOperationServiceError("STORAGE_PLAN_FIXTURE_FAILED")

    async def execute(
        self,
        operation_id: str,
        *,
        session_id: str,
        created_by: str,
    ) -> Never:
        del operation_id, session_id, created_by
        raise StorageOperationServiceError("STORAGE_EXECUTE_FIXTURE_FAILED")

    async def get(self, operation_id: str) -> None:
        del operation_id
        return None

    async def verification(self, operation_id: str) -> None:
        del operation_id
        return None


def _request(service: _FailingStorageService) -> Request:
    app = FastAPI()
    app.state.storage_operation_service = service
    return Request(
        {
            "type": "http",
            "app": app,
            "state": {"session_id": "coverage-session-1234"},
            "headers": [],
            "method": "POST",
            "path": "/",
        }
    )


async def test_storage_api_maps_service_failures_and_missing_records() -> None:
    request = _request(_FailingStorageService())

    with pytest.raises(AresProblem) as layout_problem:
        await routes.layout(request, target_disk="/fixture.img")
    assert layout_problem.value.status == 503
    assert layout_problem.value.code == "STORAGE_LAYOUT_FIXTURE_FAILED"

    payload = StorageOperationPlanRequest(
        operation="create",
        target_disk="/fixture.img",
        size_bytes=8 * 1024 * 1024,
        table_type=PartitionTableType.GPT,
    )
    with pytest.raises(AresProblem) as plan_problem:
        await routes.plan(payload, request)
    assert plan_problem.value.status == 422
    assert plan_problem.value.code == "STORAGE_PLAN_FIXTURE_FAILED"

    with pytest.raises(AresProblem) as execute_problem:
        await routes.execute("operation-fixture-1234", request)
    assert execute_problem.value.status == 409
    assert execute_problem.value.code == "STORAGE_EXECUTE_FIXTURE_FAILED"

    with pytest.raises(AresProblem) as missing_operation:
        await routes.operation("operation-missing-1234", request)
    assert missing_operation.value.status == 404
    assert missing_operation.value.code == "STORAGE_OPERATION_NOT_FOUND"

    with pytest.raises(AresProblem) as missing_verification:
        await routes.verification("operation-missing-1234", request)
    assert missing_verification.value.status == 404
    assert missing_verification.value.code == "STORAGE_VERIFICATION_NOT_FOUND"


async def test_storage_verification_postcheck_rejects_missing_verification() -> None:
    postcheck = _StorageVerificationPostcheck()
    assert await postcheck({}, {}) is False
    assert await postcheck({"verification": {"status": "not-a-status"}}, {}) is False


async def test_validate_authorized_storage_action_rejects_each_security_boundary(
    tmp_path: object,
) -> None:
    from pathlib import Path

    path = Path(str(tmp_path))
    image = path / "action-security.img"
    with image.open("wb") as handle:
        handle.truncate(128 * 1024 * 1024)
    _, store, _, _, _, plan = await _validated_create(path / "state", image)
    action = ValidateAuthorizedStorageOperationAction(store)
    context, _, _ = _context(path / "context")

    checkpoint = plan.protection_checkpoint
    assert checkpoint is not None
    valid = StorageOperationExecutionInput(
        operation_id=plan.operation_id,
        plan_id=plan.id,
        session_id=plan.session_id,
        protected_resource_id=plan.protected_resource_id,
        protected_resource_fingerprint_sha256=plan.target_disk.fingerprint_sha256,
    )

    missing = valid.model_copy(update={"operation_id": "operation-missing-1234"})
    with pytest.raises(ActionError, match="STORAGE_OPERATION_NOT_FOUND"):
        await action.run(missing.model_dump(mode="json"), context)

    with pytest.raises(ActionError, match="STORAGE_OPERATION_NOT_AUTHORIZED"):
        await action.run(valid.model_dump(mode="json"), context)

    record = await store.get_record(plan.operation_id)
    assert record is not None
    await store.put_transaction(
        record.transaction.model_copy(update={"status": StorageTransactionStatus.AUTHORIZED})
    )

    wrong_session = valid.model_copy(update={"session_id": "different-session-1234"})
    with pytest.raises(ActionError, match="STORAGE_OPERATION_SESSION_MISMATCH"):
        await action.run(wrong_session.model_dump(mode="json"), context)

    tampered = plan.model_copy(update={"fingerprint_sha256": "0" * 64})
    await store.put_plan(tampered)
    with pytest.raises(ActionError, match="STORAGE_OPERATION_PLAN_TAMPERED"):
        await action.run(valid.model_dump(mode="json"), context)

    await store.put_plan(plan)
    wrong_resource = valid.model_copy(update={"protected_resource_id": "resource-wrong-1234"})
    with pytest.raises(ActionError, match="STORAGE_PROTECTION_CHECKPOINT_INVALID"):
        await action.run(wrong_resource.model_dump(mode="json"), context)
