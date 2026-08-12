from pathlib import Path


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, count), encoding="utf-8")


executor = "backend/src/ares/recovery/executor.py"
replace(
    executor,
    "from ares.recovery.checkpoints import RecoveryCheckpointProvider\n",
    "from ares.recovery.checkpoints import RecoveryCheckpointProvider\n"
    "from ares.recovery.integrity import recovery_operation_fingerprint\n",
)
replace(
    executor,
    "    operation_id: str\n    session_id: str\n",
    "    operation_id: str\n"
    "    operation_fingerprint_sha256: str = Field(pattern=r\"^[a-f0-9]{64}$\")\n"
    "    session_id: str\n",
)
replace(
    executor,
    "    checkpoint_id: str\n    expires_at: datetime\n",
    "    checkpoint_id: str\n    operator_uid: int | None = None\n    expires_at: datetime\n",
)
replace(
    executor,
    "    async def execute(\n        self,\n        operation: RecoveryOperation,\n        *,\n        root: Path,\n        grant: RecoveryAuthorizationGrant,\n    ) -> tuple[str, ...]: ...\n",
    "    async def execute(\n"
    "        self,\n"
    "        operation: RecoveryOperation,\n"
    "        *,\n"
    "        root: Path,\n"
    "        grant: RecoveryAuthorizationGrant,\n"
    "    ) -> tuple[str, ...]: ...\n\n"
    "    async def rollback(self, operation: RecoveryOperation, *, root: Path) -> None: ...\n",
)
replace(
    executor,
    "        grant = RecoveryAuthorizationGrant(\n            operation_id=operation.operation_id,\n            session_id=session_id,\n",
    "        grant = RecoveryAuthorizationGrant(\n"
    "            operation_id=operation.operation_id,\n"
    "            operation_fingerprint_sha256=recovery_operation_fingerprint(operation),\n"
    "            session_id=session_id,\n",
)
replace(
    executor,
    "            checkpoint_id=checkpoint.id,\n            expires_at=datetime.now(UTC) + timedelta(minutes=5),\n",
    "            checkpoint_id=checkpoint.id,\n"
    "            operator_uid=1000,\n"
    "            expires_at=datetime.now(UTC) + timedelta(minutes=5),\n",
)
replace(
    executor,
    "            or grant.operation_id != operation.operation_id\n            or grant.expires_at <= datetime.now(UTC)\n",
    "            or grant.operation_id != operation.operation_id\n"
    "            or grant.operation_fingerprint_sha256 != recovery_operation_fingerprint(operation)\n"
    "            or grant.expires_at <= datetime.now(UTC)\n",
)
local_end = "        raise RecoveryExecutorError(\"RECOVERY_OPERATION_UNSUPPORTED\")\n\n\nclass UnixBrokerRecoveryExecutor:\n"
replace(
    executor,
    local_end,
    "        raise RecoveryExecutorError(\"RECOVERY_OPERATION_UNSUPPORTED\")\n\n"
    "    async def rollback(self, operation: RecoveryOperation, *, root: Path) -> None:\n"
    "        if operation.strategy is not RecoveryStrategyKind.CONFIGURATION:\n"
    "            raise RecoveryExecutorError(\"RECOVERY_ROLLBACK_UNSUPPORTED\")\n"
    "        change = ConfigurationDiff.model_validate(operation.payload.get(\"configuration_diff\"))\n"
    "        try:\n"
    "            await asyncio.to_thread(self.configuration.rollback, root, change)\n"
    "        except RecoveryToolError as exc:\n"
    "            raise RecoveryExecutorError(exc.code) from exc\n\n\n"
    "class UnixBrokerRecoveryExecutor:\n",
)
unix_anchor = "    async def _request(\n        self,\n        request: dict[str, Any],\n"
replace(
    executor,
    unix_anchor,
    "    async def rollback(self, operation: RecoveryOperation, *, root: Path) -> None:\n"
    "        result = await self._request(\n"
    "            {\n"
    "                \"action\": \"recovery.rollback\",\n"
    "                \"operation\": operation.model_dump(mode=\"json\"),\n"
    "                \"root\": str(root),\n"
    "            }\n"
    "        )\n"
    "        if result.get(\"rolled_back\") is not True:\n"
    "            raise RecoveryExecutorError(\"RECOVERY_ROLLBACK_FAILED\")\n\n"
    + unix_anchor,
)

broker = "backend/src/ares/runtime/recovery_broker.py"
replace(
    broker,
    "or checkpoint.provider_id != operation.operation_id",
    "or checkpoint.backup_id != operation.operation_id",
)

store = "backend/src/ares/recovery/store.py"
replace(
    store,
    "        if case.status not in {RecoveryStatus.RECOVERING, RecoveryStatus.VERIFYING}:\n            return case\n",
    "        if case.status in {RecoveryStatus.AUTHORIZED, RecoveryStatus.PROTECTED}:\n"
    "            if case.recovery_plan is None:\n"
    "                return case\n"
    "            lost = tuple(\n"
    "                item.model_copy(\n"
    "                    update={\n"
    "                        \"status\": (\n"
    "                            RecoveryOperationStatus.ABORTED\n"
    "                            if item.status in {\n"
    "                                RecoveryOperationStatus.AUTHORIZATION_PENDING,\n"
    "                                RecoveryOperationStatus.AUTHORIZED,\n"
    "                            }\n"
    "                            else item.status\n"
    "                        ),\n"
    "                        \"error_code\": (\n"
    "                            \"RECOVERY_AUTHORIZATION_LOST_ON_RESTART\"\n"
    "                            if item.status in {\n"
    "                                RecoveryOperationStatus.AUTHORIZATION_PENDING,\n"
    "                                RecoveryOperationStatus.AUTHORIZED,\n"
    "                            }\n"
    "                            else item.error_code\n"
    "                        ),\n"
    "                    }\n"
    "                )\n"
    "                for item in case.recovery_plan.operations\n"
    "            )\n"
    "            if any(item.status is RecoveryOperationStatus.ABORTED for item in lost):\n"
    "                plan = case.recovery_plan.model_copy(update={\"operations\": lost})\n"
    "                return case.model_copy(\n"
    "                    update={\n"
    "                        \"status\": RecoveryStatus.ABORTED,\n"
    "                        \"recovery_plan\": plan,\n"
    "                        \"updated_at\": datetime.now(UTC),\n"
    "                        \"final_state\": (\n"
    "                            \"Pending one-use recovery authorization was lost on restart.\"\n"
    "                        ),\n"
    "                    }\n"
    "                )\n"
    "            return case\n"
    "        if case.status not in {RecoveryStatus.RECOVERING, RecoveryStatus.VERIFYING}:\n"
    "            return case\n",
)

orchestrator = "backend/src/ares/recovery/orchestrator.py"
replace(orchestrator, "import hashlib\n", "")
replace(
    orchestrator,
    "                issues.append(\n                    RecoveryIssue(\n                        code=code,",
    "                issues.append(\n                    RecoveryIssue(\n                        code=code,",
)
replace(
    orchestrator,
    "            for item in boot_result.issues:\n",
    "            root_partition = boot_result.environment.root_partition\n"
    "            if root_partition is not None and root_partition.uuid:\n"
    "                evidence.append(\n"
    "                    DiagnosticEvidence(\n"
    "                        source=\"boot.diagnose\",\n"
    "                        severity=EvidenceSeverity.INFO,\n"
    "                        subsystem=\"filesystem\",\n"
    "                        event=\"root-filesystem-identity\",\n"
    "                        normalized_message=\"observed root filesystem UUID\",\n"
    "                        confidence=0.99,\n"
    "                        resource_ids=(root_partition.resource_id,),\n"
    "                        metadata={\"root_uuid\": root_partition.uuid},\n"
    "                    )\n"
    "                )\n"
    "            for item in boot_result.issues:\n",
)
replace(
    orchestrator,
    "            except (RecoveryOrchestratorError, Exception) as exc:\n",
    "            except Exception as exc:\n",
)

# Roll back safe configuration writes when their postcheck fails.
actions = "backend/src/ares/actions/recovery.py"
replace(
    actions,
    "        verification = await self._verify(operation, root)\n        if not verification[\"valid\"]:\n            raise ActionError(str(verification[\"error_code\"]))\n",
    "        verification = await self._verify(operation, root)\n"
    "        if not verification[\"valid\"]:\n"
    "            if operation.rollback_supported:\n"
    "                try:\n"
    "                    await self.executor.rollback(operation, root=root)\n"
    "                except RecoveryExecutorError as exc:\n"
    "                    raise ActionError(\"RECOVERY_ROLLBACK_FAILED\") from exc\n"
    "                raise ActionError(\"RECOVERY_OPERATION_ROLLED_BACK\")\n"
    "            raise ActionError(str(verification[\"error_code\"]))\n",
)
