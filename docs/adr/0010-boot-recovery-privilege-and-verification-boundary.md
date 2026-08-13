# ADR-0010 — Boot Recovery privilege and verification boundary

- Status: Accepted
- Date: 2026-08-12

## Context

Boot repair can make an otherwise recoverable machine unbootable. The existing ARES architecture already separates typed Capabilities, independent consent, the root Tool Broker, ProtectionCheckpoint, Audit Ledger and durable transaction state. Boot Recovery must extend those mechanisms instead of creating a command-oriented rescue path.

A second constraint is epistemic: validating files from a live/recovery environment is not equivalent to proving that a subsequent firmware boot succeeds.

## Decision

ARES implements boot recovery with two distinct capabilities:

- `boot.diagnose`, read-only and evidence-producing;
- `boot.repair.grub`, high-risk and mutating.

The mutating capability is limited to supported Debian-family GRUB recovery. Windows Boot Manager and systemd-boot may be detected but are not modified automatically.

Every GRUB mutation requires an immutable/fingerprinted `BootRepairPlan`, a boot-state `ProtectionCheckpoint`, an independent local consent challenge and a one-use authorization grant. The grant is bound to the exact plan fingerprint and session. Mutation is executed only through `BootBroker`, delegated by the existing `ares-tool-broker` process.

`BootBroker` exposes semantic operations only. API, browser, Agent and LLM cannot provide a shell string, executable or arbitrary argv. The broker revalidates target-disk identity, plan integrity, checkpoint binding and grant validity immediately before mutation.

The checkpoint protects selected boot state and is explicitly not represented as a full filesystem/data backup.

Offline postchecks may report internal consistency, but without reboot evidence they cannot report full boot success. A statically consistent repaired chain therefore remains `PARTIAL` with `LIMITED` confidence. Interrupted mutation/verification is reconciled to `UNKNOWN`; `UNKNOWN` is reinspected and is never automatically retried.

## Consequences

Positive:

- Boot Recovery reuses the established privilege and consent boundary.
- The LLM cannot obtain an indirect shell through boot repair.
- Target substitution, stale plans and replayed grants fail closed.
- Verification language remains aligned with evidence actually available.
- A future higher-level recovery orchestrator can compose Boot Recovery as a child capability while retaining its independent authorization and audit trail.

Costs and limitations:

- A browser cannot approve its own repair request; trusted local consent is required.
- A boot-state checkpoint cannot guarantee rollback for every firmware/bootloader side effect.
- Full success requires evidence beyond offline static inspection.
- Distribution-specific adapters are required; support must not be inferred from detection alone.

## Rejected alternatives

### Let the Agent run `grub-install` directly

Rejected because it bypasses Capability scope, target revalidation, consent, checkpointing and audit.

### One generic `boot.repair` shell wrapper

Rejected because it would turn structured policy into an argv transport and would make minimal intervention difficult to enforce.

### Treat a successful command exit as successful recovery

Rejected because command completion does not prove that the firmware-to-root boot chain works.

### Treat the boot checkpoint as a full backup

Rejected because it protects only selected boot artifacts and would overstate recoverability of unrelated data.
