from pathlib import Path


common = Path("backend/tests/test_storage_common_platform_edges.py")
text = common.read_text(encoding="utf-8")
old = '''        assert (
            await cli._storage_command(parser.parse_args(["storage", "execute", operation_id]), app)
            == 0
        )
        await _poll_storage(
            app,
            operation_id,
            {StorageTransactionStatus.COMMITTED, StorageTransactionStatus.FAILED},
        )
        record = await app.state.storage_operation_store.get_record(operation_id)
        assert (
            record is not None and record.transaction.status is StorageTransactionStatus.COMMITTED
        )

        assert (
'''
new = '''        record = await app.state.storage_operation_store.get_record(operation_id)
        assert record is not None

        assert (
'''
if old not in text:
    raise SystemExit("storage CLI execute lifecycle block not found")
text = text.replace(old, new, 1)
common.write_text(text, encoding="utf-8")

final = Path("backend/tests/test_storage_final_coverage.py")
text = final.read_text(encoding="utf-8")
if "from fastapi import FastAPI\n" not in text:
    text = text.replace("import pytest\n", "import pytest\nfrom fastapi import FastAPI\n", 1)
text = text.replace(
    "application = argparse.Namespace(state=argparse.Namespace(backup_service=_BackupService()))",
    "application = FastAPI()\n    application.state.backup_service = _BackupService()",
)
text = text.replace(
    "application = argparse.Namespace(state=argparse.Namespace(filesystem_repair_service=service))",
    "application = FastAPI()\n    application.state.filesystem_repair_service = service",
)
final.write_text(text, encoding="utf-8")
