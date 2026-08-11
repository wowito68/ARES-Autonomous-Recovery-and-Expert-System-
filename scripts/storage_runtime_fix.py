from pathlib import Path


broker = Path("backend/src/ares/runtime/storage_broker.py")
text = broker.read_text(encoding="utf-8")
old = '''        if action == "storage.verify":
            plan = StorageOperationPlan.model_validate(request.get("plan"))
            await self._validate_preflight_plan(plan, checkpoint_required=True)
            return (await self.tools.verify_tool.verify(plan)).model_dump(mode="json")
'''
new = '''        if action == "storage.verify":
            plan = StorageOperationPlan.model_validate(request.get("plan"))
            await self._validate_postwrite_plan(plan)
            verification = await self.tools.verify_tool.verify(plan)
            await self.audit.append(
                event_type="storage.verification.completed",
                source="ares-tool-broker",
                correlation_id=plan.operation_id,
                session_id=plan.session_id,
                payload={
                    "target_fingerprint": plan.target_disk.fingerprint_sha256,
                    "status": verification.status.value,
                    "before_layout": verification.before_layout_fingerprint_sha256,
                    "expected_layout": verification.expected_layout_fingerprint_sha256,
                    "after_layout": verification.after_layout_fingerprint_sha256,
                },
            )
            return verification.model_dump(mode="json")
'''
if old not in text:
    raise SystemExit("storage.verify dispatch block not found")
text = text.replace(old, new, 1)
marker = '''    async def _audit_failure(self, plan: StorageOperationPlan, exc: BaseException) -> None:
'''
method = '''    async def _validate_postwrite_plan(self, plan: StorageOperationPlan) -> None:
        if not storage_plan_integrity_valid(plan) or plan.expires_at <= datetime.now(UTC):
            raise PartitionToolError("STORAGE_OPERATION_PLAN_INVALID")
        if not plan.executable or plan.dry_run is None or not plan.dry_run.valid:
            raise PartitionToolError("STORAGE_OPERATION_NOT_EXECUTABLE")
        if not self.write_gate.evaluate(plan.target_disk).allowed:
            raise PartitionToolError("PRODUCTION_STORAGE_WRITE_GATE_BLOCKED")
        current_identity = await self.tools.identity.inspect(plan.target_disk.requested_path)
        if current_identity.fingerprint_sha256 != plan.target_disk.fingerprint_sha256:
            raise PartitionToolError("STORAGE_DEVICE_IDENTITY_CHANGED")
        checkpoint = plan.protection_checkpoint
        if (
            checkpoint is None
            or checkpoint.status is not ProtectionCheckpointStatus.READY
            or checkpoint.session_id != plan.session_id
            or checkpoint.verification_id is None
            or plan.protected_resource_id not in checkpoint.protected_resources
            or checkpoint.resource_fingerprints.get(plan.protected_resource_id)
            != plan.target_disk.fingerprint_sha256
        ):
            raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")
        durable = await self.checkpoints.get(checkpoint.id)
        artifact = await self.operation_store.get_checkpoint(checkpoint.id)
        if (
            durable != checkpoint
            or artifact is None
            or artifact.operation_id != plan.operation_id
            or artifact.partition_table_fingerprint_sha256
            != plan.original_layout.partition_table.fingerprint_sha256
        ):
            raise PartitionToolError("STORAGE_PROTECTION_CHECKPOINT_INVALID")

'''
if marker not in text:
    raise SystemExit("audit failure marker not found")
text = text.replace(marker, method + marker, 1)
broker.write_text(text, encoding="utf-8")

api_test = Path("backend/tests/test_storage_operation_api.py")
text = api_test.read_text(encoding="utf-8")
old = '''            graph = await client.get("/api/v1/knowledge/graph", headers=headers)
            nodes = cast(list[dict[str, object]], cast(dict[str, object], graph.json())["nodes"])
            kinds = {node["kind"] for node in nodes}
            assert {"partition_table", "storage_transaction", "storage_verification"} <= kinds
'''
new = '''            graph_kinds: set[object] = set()
            for _ in range(100):
                graph = await client.get("/api/v1/knowledge/graph", headers=headers)
                assert graph.status_code == 200
                nodes = cast(
                    list[dict[str, object]], cast(dict[str, object], graph.json())["nodes"]
                )
                graph_kinds = {node["kind"] for node in nodes}
                if "storage_verification" in graph_kinds:
                    break
                await asyncio.sleep(0.02)
            assert {"partition_table", "storage_transaction", "storage_verification"} <= graph_kinds
'''
if old not in text:
    raise SystemExit("graph assertion block not found")
api_test.write_text(text.replace(old, new, 1), encoding="utf-8")
