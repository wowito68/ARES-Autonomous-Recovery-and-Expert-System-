from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"post-fix anchor missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "backend/src/ares/api/routes/assistant.py",
    "Conoces boot.diagnose y boot.repair.grub. Ante 'mi Linux no arranca' debes comenzar por diagnóstico read-only y evidencia de firmware, bootloader, ESP, kernel, initramfs y root; nunca por reinstalar GRUB. La reparación automática se limita a GRUB en sistemas Debian-family y exige BootRepairPlan exacto, checkpoint de estado de arranque, consentimiento local independiente y broker root. Windows Boot Manager y systemd-boot se detectan pero no se reparan automáticamente. fstab se analiza pero no se modifica desde Boot Recovery. Una verificación offline no prueba un reinicio real: si falta esa evidencia, reporta PARTIAL/LIMITED y no afirmes que el sistema ya arranca. Un estado UNKNOWN exige reconciliación, no reintento.\n",
    "Conoces boot.diagnose y boot.repair.grub. Ante 'mi Linux no arranca' debes comenzar por\n"
    "diagnóstico read-only y evidencia de firmware, bootloader, ESP, kernel, initramfs y root; nunca\n"
    "por reinstalar GRUB. La reparación automática se limita a GRUB en sistemas Debian-family y\n"
    "exige BootRepairPlan exacto, checkpoint de estado de arranque, consentimiento local\n"
    "independiente y broker root. Windows Boot Manager y systemd-boot se detectan pero no se\n"
    "reparan automáticamente. fstab se analiza pero no se modifica desde Boot Recovery. Una\n"
    "verificación offline no prueba un reinicio real: si falta esa evidencia, reporta\n"
    "PARTIAL/LIMITED y no afirmes que el sistema ya arranca. Un estado UNKNOWN exige\n"
    "reconciliación, no reintento.\n",
)

replace_once(
    "backend/src/ares/cli.py",
    '                "Type REQUEST to create the boot checkpoint and request independent authorization: ",\n',
    '                "Type REQUEST to create the boot checkpoint and request "\n'
    '                "independent authorization: ",\n',
)
replace_once(
    "backend/src/ares/cli.py",
    '                        "Independent authorization required. Run locally:\n"\n',
    '                        "Independent authorization required. Run locally:\\n"\n',
)

replace_once(
    "backend/tests/test_boot_platform_integration.py",
    '    assert parser.parse_args(["boot", "diagnose", "--root-path", "/mnt/linux"]).boot_command == "diagnose"\n',
    '    parsed_diagnose = parser.parse_args(["boot", "diagnose", "--root-path", "/mnt/linux"])\n'
    '    assert parsed_diagnose.boot_command == "diagnose"\n',
)

replace_once(
    "backend/src/ares/runtime/consent.py",
    '                "firmware_mode": plan.boot_impact.firmware_mode,\n',
    '                "boot_impact": plan.boot_impact.level.value,\n',
)
replace_once(
    "backend/src/ares/runtime/broker.py",
    '    boot_broker = BootBroker(\n'
    '        BootRepairToolSuite(),\n'
    '        DiskIdentityTool(),\n'
    '        audit,\n'
    '        consent,\n'
    '        checkpoint_store,\n'
    '        boot_store,\n'
    '    )\n',
    '    boot_broker = BootBroker(\n'
    '        tools=BootRepairToolSuite(),\n'
    '        identity=DiskIdentityTool(),\n'
    '        audit=audit,\n'
    '        consent=consent,\n'
    '        checkpoints=checkpoint_store,\n'
    '        store=boot_store,\n'
    '    )\n',
)

replace_once(
    "backend/src/ares/cli.py",
    '            result = await service.plan(\n',
    '            repair_plan = await service.plan(\n',
)
replace_once(
    "backend/src/ares/cli.py",
    '            print(result.model_dump_json(indent=2))\n            return 0 if result.executable else 3\n',
    '            print(repair_plan.model_dump_json(indent=2))\n'
    '            return 0 if repair_plan.executable else 3\n',
)
replace_once(
    "backend/src/ares/cli.py",
    '            result = await service.reconcile(repair_id, session_id=record.execution.session_id)\n'
    '            print(result.model_dump_json(indent=2))\n',
    '            reconciliation = await service.reconcile(\n'
    '                repair_id, session_id=record.execution.session_id\n'
    '            )\n'
    '            print(reconciliation.model_dump_json(indent=2))\n',
)
