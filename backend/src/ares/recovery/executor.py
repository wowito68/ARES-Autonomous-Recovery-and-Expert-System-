"""Semantic mutation executor for package, configuration and initramfs recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

from ares.recovery.models import ConfigurationDiff, RecoveryOperation, RecoveryStrategyKind
from ares.tools.recovery import (
    AptPackageManagerAdapter,
    ConfigurationRecoveryTool,
    InitramfsRecoveryTool,
    RecoveryProcessRunner,
    RecoveryToolError,
)

_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESPONSE_BYTES = 4_000_000


class RecoveryExecutorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryMutationExecutor(Protocol):
    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
    ) -> tuple[str, ...]: ...


class LocalTestRecoveryExecutor:
    def __init__(self, runner: RecoveryProcessRunner) -> None:
        self.packages = AptPackageManagerAdapter(runner)
        self.configuration = ConfigurationRecoveryTool()
        self.initramfs = InitramfsRecoveryTool(runner)

    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
    ) -> tuple[str, ...]:
        try:
            if operation.strategy is RecoveryStrategyKind.CONFIGURATION:
                change = ConfigurationDiff.model_validate(operation.payload.get("configuration_diff"))
                backup, digest = await asyncio.to_thread(self.configuration.apply, root, change)
                return (f"configuration backup: {backup}", f"configuration sha256: {digest}")
            if operation.strategy is RecoveryStrategyKind.PACKAGE:
                package_operation = str(operation.payload.get("package_operation", ""))
                return await self.packages.repair(root, package_operation)
            if operation.strategy is RecoveryStrategyKind.INITRAMFS:
                kernel = str(operation.payload.get("kernel_version", ""))
                return await self.initramfs.rebuild(root, kernel)
        except RecoveryToolError as exc:
            raise RecoveryExecutorError(exc.code) from exc
        raise RecoveryExecutorError("RECOVERY_OPERATION_UNSUPPORTED")


class UnixBrokerRecoveryExecutor:
    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 240.0,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def execute(
        self,
        operation: RecoveryOperation,
        *,
        root: Path,
    ) -> tuple[str, ...]:
        result = await self._request(
            {
                "action": "recovery.execute",
                "operation": operation.model_dump(mode="json"),
                "root": str(root),
            }
        )
        changes = result.get("changes")
        if not isinstance(changes, list) or not all(isinstance(item, str) for item in changes):
            raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
        return tuple(changes)

    async def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise RecoveryExecutorError("RECOVERY_BROKER_REQUEST_TOO_LARGE")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=3.0
            )
        except (OSError, TimeoutError) as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_UNAVAILABLE") from exc
        try:
            writer.write(encoded + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
            if not raw:
                raise RecoveryExecutorError("RECOVERY_BROKER_DISCONNECTED")
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_TOO_LARGE")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID") from exc
            if not isinstance(message, dict):
                raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
            if message.get("type") == "error":
                code = message.get("code")
                raise RecoveryExecutorError(code if isinstance(code, str) else "RECOVERY_BROKER_FAILED")
            payload = message.get("payload")
            if message.get("type") != "result" or not isinstance(payload, dict):
                raise RecoveryExecutorError("RECOVERY_BROKER_RESPONSE_INVALID")
            return {str(key): value for key, value in payload.items()}
        except TimeoutError as exc:
            raise RecoveryExecutorError("RECOVERY_BROKER_TIMEOUT") from exc
        finally:
            writer.close()
            await writer.wait_closed()
