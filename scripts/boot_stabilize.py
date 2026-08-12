from pathlib import Path


def replace_exact(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}: {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


tools = Path("backend/src/ares/tools/boot.py")
replace_exact(
    tools,
    "import asyncio\nimport hashlib",
    "import asyncio\nimport hashlib\nfrom contextlib import suppress",
)
replace_exact(tools, "    BootPartition,\n", "")
replace_exact(
    tools,
    "        try:\n            Path(environment.work_root).rmdir()\n        except OSError:\n            pass\n",
    "        with suppress(OSError):\n            await asyncio.to_thread(Path(environment.work_root).rmdir)\n",
)
replace_exact(
    tools,
    "            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,",
    "            stdin=(\n                asyncio.subprocess.PIPE\n                if input_bytes is not None\n                else asyncio.subprocess.DEVNULL\n            ),",
)
replace_exact(
    tools,
    "                repair_supported=kind is BootloaderKind.GRUB and family is DistributionFamily.DEBIAN,",
    "                repair_supported=(\n                    kind is BootloaderKind.GRUB\n                    and family is DistributionFamily.DEBIAN\n                ),",
)

engine = Path("backend/src/ares/boot/engine.py")
replacements = {
    "        root_partition = _root_partition(layout, operating_systems[0] if operating_systems else None)": (
        "        root_partition = _root_partition(\n"
        "            layout, operating_systems[0] if operating_systems else None\n"
        "        )"
    ),
    '                    "last_known_stage": "boot-repair-unknown" if uncertain else "boot-repair-failed",': (
        '                    "last_known_stage": (\n'
        '                        "boot-repair-unknown"\n'
        '                        if uncertain\n'
        '                        else "boot-repair-failed"\n'
        '                    ),'
    ),
    "    return max(issues, key=lambda item: order[item.severity]).severity if issues else BootIssueSeverity.INFO": (
        "    if not issues:\n"
        "        return BootIssueSeverity.INFO\n"
        "    return max(issues, key=lambda item: order[item.severity]).severity"
    ),
    'summary="Multiple Linux installations were detected; a repair target must be selected.",': (
        'summary=(\n                    "Multiple Linux installations were detected; "\n'
        '                    "a repair target must be selected."\n                ),'
    ),
    'summary="Windows Boot Manager was detected; automatic Windows boot repair is disabled.",': (
        'summary=(\n                    "Windows Boot Manager was detected; automatic Windows "\n'
        '                    "boot repair is disabled."\n                ),'
    ),
    'summary="systemd-boot was detected; automatic repair is not enabled in this version.",': (
        'summary=(\n                    "systemd-boot was detected; automatic repair is not enabled "\n'
        '                    "in this version."\n                ),'
    ),
    'recommended_action="Restore or reinstall a kernel before relying on bootloader repair.",': (
        'recommended_action=(\n                    "Restore or reinstall a kernel before relying on "\n'
        '                    "bootloader repair."\n                ),'
    ),
    'recommended_action="Manual fstab review is required; ARES will not edit fstab automatically.",': (
        'recommended_action=(\n                    "Manual fstab review is required; ARES will not edit "\n'
        '                    "fstab automatically."\n                ),'
    ),
    'summary="The system is running in UEFI mode but no EFI System Partition was identified.",': (
        'summary=(\n                    "The system is running in UEFI mode but no EFI System "\n'
        '                    "Partition was identified."\n                ),'
    ),
    'summary="GRUB EFI loader files exist, but no matching firmware boot entry was found.",': (
        'summary=(\n                    "GRUB EFI loader files exist, but no matching firmware "\n'
        '                    "boot entry was found."\n                ),'
    ),
    'description="Create a verified snapshot of relevant EFI, GRUB and boot configuration state.",': (
        'description=(\n                "Create a verified snapshot of relevant EFI, GRUB and boot "\n'
        '                "configuration state."\n            ),'
    ),
    'description="Install or reinstall GRUB for the approved target OS and firmware mode.",': (
        'description=(\n                        "Install or reinstall GRUB for the approved target OS "\n'
        '                        "and firmware mode."\n                    ),'
    ),
    'description="Verify firmware, ESP, bootloader, config, kernel, initramfs and root chain.",': (
        'description=(\n                    "Verify firmware, ESP, bootloader, config, kernel, initramfs "\n'
        '                    "and root chain."\n                ),'
    ),
    'return "Select which Linux installation should be recovered before creating a repair plan."': (
        'return (\n            "Select which Linux installation should be recovered before "\n'
        '            "creating a repair plan."\n        )'
    ),
    'return "Create a minimal BootRepairPlan, protect current boot state, then request authorization."': (
        'return (\n            "Create a minimal BootRepairPlan, protect current boot state, "\n'
        '            "then request authorization."\n        )'
    ),
}
for old, new in replacements.items():
    replace_exact(engine, old, new)

models = Path("backend/src/ares/boot/models.py")
replace_exact(
    models,
    "    verification_strategy: BootVerificationStrategy = Field(default_factory=BootVerificationStrategy)",
    "    verification_strategy: BootVerificationStrategy = Field(\n"
    "        default_factory=BootVerificationStrategy\n"
    "    )",
)

plugin = Path("backend/src/ares/capabilities/plugins/boot_recovery.py")
replace_exact(
    plugin,
    'reason="Inspect firmware, EFI, boot files and boot configuration without persistent mutation.",',
    'reason=(\n            "Inspect firmware, EFI, boot files and boot configuration "\n'
    '            "without persistent mutation."\n        ),',
)
replace_exact(
    plugin,
    'reason="Persist boot diagnostics, plans, checkpoints, executions and verification evidence.",',
    'reason=(\n            "Persist boot diagnostics, plans, checkpoints, executions and "\n'
    '            "verification evidence."\n        ),',
)

test_tools = Path("backend/tests/test_boot_tools.py")
replace_exact(
    test_tools,
    '    available_tools: ClassVar[set[str]] = {"efibootmgr", "mount", "umount", "grub-install", "chroot"}',
    '    available_tools: ClassVar[set[str]] = {\n'
    '        "efibootmgr",\n'
    '        "mount",\n'
    '        "umount",\n'
    '        "grub-install",\n'
    '        "chroot",\n'
    '    }',
)
