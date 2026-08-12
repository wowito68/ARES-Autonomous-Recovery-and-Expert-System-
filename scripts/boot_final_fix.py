from pathlib import Path

path = Path("backend/tests/test_boot_tool_runtime_edges.py")
text = path.read_text(encoding="utf-8")
old = '''    with pytest.raises(BootToolError, match="UMOUNT_UNAVAILABLE"):
        await RepairEnvironmentTool(runner).cleanup_paths(("/fixture",))

    runner = _ConfigurableRunner()
    runner.fail_tool = "umount"'''
new = '''    with pytest.raises(BootToolError, match="UMOUNT_UNAVAILABLE"):
        await RepairEnvironmentTool(runner).cleanup_paths(("/fixture",))
    runner.available_tools.add("umount")

    runner = _ConfigurableRunner()
    runner.fail_tool = "umount"'''
if old not in text:
    raise SystemExit("fixture anchor missing")
text = text.replace(old, new, 1)
if "import shutil\n" not in text:
    text = text.replace("from pathlib import Path\n", "from pathlib import Path\nimport shutil\n", 1)
text = text.replace("boot_tools.shutil, \"which\"", "shutil, \"which\"")
path.write_text(text, encoding="utf-8")
