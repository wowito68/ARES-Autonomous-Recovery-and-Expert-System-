# Boot Recovery validation matrix

This file records the permanent validation surface for Boot Recovery. It is intentionally separate from any temporary formatter or diagnostic workflow.

The automated suite covers:

- firmware and bootloader detection in BIOS and UEFI fixtures;
- Debian-family GRUB detection and controlled fixture repair;
- missing GRUB, missing initramfs and missing EFI entry evidence;
- Windows Boot Manager and systemd-boot detection without unsupported mutation;
- fstab analysis remaining read-only;
- immutable/fingerprinted repair plans, expiry and session binding;
- boot-state checkpoint creation and durable ProtectionCheckpoint binding;
- independent consent challenges and one-use authorization grants;
- target disk identity revalidation before privileged mutation;
- root `BootBroker` authorization, execution, audit completion and reconciliation-required behavior;
- Unix broker protocol success, malformed responses, timeout, disconnect and unavailable socket failures;
- Service/Capability/Workflow/Action integration;
- Knowledge Graph diagnosis, repair and verification projection;
- API success, Problem Details and not-found behavior;
- CLI diagnose/plan/repair/status/verify/cancel/reconcile control flow;
- durable `UNKNOWN`, `ABORTED`, `REPAIR_FAILED` and `COMPLETED` lifecycle semantics;
- offline verification remaining `PARTIAL/LIMITED` without reboot proof;
- Agent/Reasoning selection of diagnosis before repair;
- frontend JavaScript syntax through the permanent Backend CI workflow.

No destructive test is executed against the runner operating system or a developer workstation. Mutation tests use controlled temporary filesystem fixtures and ARES test-mode executors.

The repository-wide coverage threshold remains 85%; Boot Recovery is not excluded and the threshold is not reduced for this increment.
