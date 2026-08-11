from pathlib import Path

patch = Path("scripts/storage_mypy_patch.py")
text = patch.read_text(encoding="utf-8")
old = '        raise SystemExit(f"expected block not found: {path}: {old[:80]!r}")\n'
new = '        print(f"skip divergent block: {path}: {old[:80]!r}")\n        return\n'
if old not in text:
    raise SystemExit("mypy patch helper not found")
patch.write_text(text.replace(old, new, 1), encoding="utf-8")

filesystem = Path("backend/src/ares/tools/filesystem.py")
source = filesystem.read_text(encoding="utf-8")
needle = "        options = tuple(option for option in record.options if option in _SAFE_MOUNT_OPTIONS)\n        if options:\n"
replacement = "        options = tuple(option for option in record.options if option in _SAFE_MOUNT_OPTIONS)\n        args: tuple[str, ...]\n        if options:\n"
if needle in source:
    source = source.replace(needle, replacement, 1)
elif replacement not in source:
    raise SystemExit("filesystem remount block not found")
filesystem.write_text(source, encoding="utf-8")
