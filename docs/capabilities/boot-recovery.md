# Boot Recovery & Bootloader Management

## Status

Implemented vertical slice on top of the ARES Capability/Workflow/Tool architecture. Automatic mutation is deliberately limited to GRUB recovery for supported Debian-family Linux installations. Windows Boot Manager and systemd-boot are detected but not automatically repaired.

## Capabilities

### `boot.diagnose@1.0.0`

- `operation=observe`
- `mode=read_only`
- `risk=low`
- collects structured firmware, EFI, bootloader, OS, kernel, initramfs, root-filesystem and fstab evidence;
- reuses the Storage Operation Engine for target-disk/layout identity;
- never repairs while diagnosing.

### `boot.repair.grub@1.0.0`

- `operation=recover`
- `mode=mutating`
- `risk=high`
- exact `BootRepairPlan` with SHA-256 fingerprint and expiry;
- boot-state ProtectionCheckpoint before authorization;
- independent local consent challenge;
- one-use grant bound to plan/session/fingerprint;
- semantic execution through the root Tool Broker;
- mandatory post-operation verification.

## Evidence model

`BootDiagnosticResult` records a typed `BootEnvironment` and issues rather than free-form command output. Evidence includes firmware mode, EFI entries, detected bootloaders, Linux installations, target disk identity, ESP/root partition, kernels, initramfs images, GRUB configuration and fstab analysis.

No raw LLM-generated command or argv reaches the Tool Layer.

## Minimal intervention

The plan is derived from observed issues. ARES does not reinstall GRUB merely because a system failed to boot. For example, an absent EFI entry can be represented as a narrower semantic operation than a full GRUB installation when the evidence supports it.

Unsupported or ambiguous situations make the plan non-executable. Multiple Linux installations require explicit target selection. Windows Boot Manager is never modified by this Capability.

## Protection checkpoint

The checkpoint is a boot-state snapshot, not a full data backup. It protects selected boot artifacts and their hashes, including applicable EFI/GRUB/configuration/kernel/initramfs state. It is promoted into the existing `ProtectionCheckpoint` contract so the Capability Manager and broker both enforce protection.

The checkpoint does not claim to preserve arbitrary user files or make every boot mutation reversible.

## Privilege boundary

Production mutation follows:

```text
API / CLI / Agent intent
        -> BootRepairPlan
        -> ProtectionCheckpoint
        -> independent consent
        -> one-use authorization grant
        -> ares-tool-broker
        -> BootRepairToolSuite
        -> verification
```

`BootBroker` accepts only semantic `boot.checkpoint`, `boot.authorize`, `boot.execute` and `boot.verify` requests. It revalidates target identity, plan integrity, checkpoint binding and grant validity. It never accepts a shell string or caller-provided executable.

## Debian-family GRUB support

The real adapter is deliberately bounded to Debian-family systems. It uses allowlisted local tools required by the exact plan and can regenerate GRUB configuration and initramfs where the plan explicitly requires them.

Fedora-family detection does not imply Fedora repair support. No automatic Windows or systemd-boot repair is implemented.

## fstab

Boot Recovery analyzes fstab references as evidence only. It does not edit `/etc/fstab`; configuration repair belongs to a separate controlled recovery capability.

## Verification semantics

Offline inspection can establish that boot-chain artifacts are internally consistent, but it cannot prove that firmware will complete a real reboot. Therefore an offline successful static check is reported as `PARTIAL` with `LIMITED` confidence, never as fully verified boot success.

A process interruption during `EXECUTING` or `VERIFYING` is reconciled to `UNKNOWN`. A lost pending authorization after restart becomes `ABORTED`. `UNKNOWN` requires explicit reinspection and never automatic mutation retry.

## API

- `POST /api/v1/boot/diagnose`
- `POST /api/v1/boot/repair/plan`
- `POST /api/v1/boot/repair`
- `GET /api/v1/boot/repairs/{repair_id}`
- `GET /api/v1/boot/repairs/{repair_id}/verification`
- `POST /api/v1/boot/repairs/{repair_id}/cancel`
- `POST /api/v1/boot/repairs/{repair_id}/reconcile`

The browser can request authorization but cannot approve its own challenge.

## CLI

- `ares boot diagnose`
- `ares boot plan <diagnostic-id>`
- `ares boot repair <plan-id>`
- `ares boot status <repair-id>`
- `ares boot verify <repair-id>`
- `ares boot cancel <repair-id>`
- `ares boot reconcile <repair-id>`

Trusted approval remains in `ares consent approve <challenge-id>`.

## Frontend and Agent

The Boot Recovery UI displays evidence, issues, plan fingerprint, exact operations, protection and verification limitations. It cannot grant consent.

The Agent is instructed to diagnose first when a user reports that Linux does not boot. It must not infer that GRUB should be reinstalled, must not bypass independent consent and must not claim successful reboot from offline evidence.

## Known limitations

- Debian-family GRUB only for automatic bootloader repair.
- No automatic Windows Boot Manager repair.
- No automatic systemd-boot repair.
- No automatic fstab modification.
- No proof of successful reboot without reboot evidence.
- No generic rollback guarantee for bootloader mutation.
- Pending consent challenges remain dependent on the local consent service lifecycle.
- Production behavior assumes the required allowlisted boot utilities are present in the ARES image; absence fails closed.
