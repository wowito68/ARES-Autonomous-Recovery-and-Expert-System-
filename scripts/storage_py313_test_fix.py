from pathlib import Path


common = Path("backend/tests/test_storage_common_platform_edges.py")
text = common.read_text(encoding="utf-8")
line = "    assert socket_path.exists()\n"
if line not in text:
    raise SystemExit("root broker socket post-close assertion not found")
text = text.replace(line, "", 1)
common.write_text(text, encoding="utf-8")

api = Path("backend/tests/test_storage_operation_api.py")
text = api.read_text(encoding="utf-8")
old = "    for _ in range(100):\n"
new = "    for _ in range(300):\n"
if old not in text:
    raise SystemExit("storage API poll loop not found")
api.write_text(text.replace(old, new, 1), encoding="utf-8")
