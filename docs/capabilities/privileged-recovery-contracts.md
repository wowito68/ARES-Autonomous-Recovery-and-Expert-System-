# Privileged recovery contracts

Status: catalog and planning contracts installed as `ares.recovery-core@0.1.0`;
mutation providers intentionally disabled.

This increment makes the recovery roadmap machine-readable without claiming that ARES can
already perform privileged repairs. Every capability is visible through the public catalog
with its risk, semantic schema, evidence, checkpoint, authorization, verification and rollback
requirements. `enabled=false` and `disabled_reason` identify the exact missing broker boundary.

| Capability | Risk | Provider required before enablement |
| --- | --- | --- |
| `boot.repair-grub` | Critical | `boot.*` mount namespace, BIOS/UEFI installer, atomic cleanup and boot verification |
| `boot.repair-efi-entry` | High | efivarfs/NVRAM broker, signed-loader validation and BootOrder rollback |
| `boot.rebuild-initramfs` | High | closed installed-system namespace, kernel resolution and atomic image replacement |
| `boot.repair-fstab` | High | typed fstab editor, UUID resolution, atomic write and mount dry-run |
| `kernel.rollback` | High | kernel transaction broker, last-known-good evidence and boot test |
| `kernel.reinstall` | High | pinned trusted package transaction plus initramfs/GRUB verification |
| `packages.rollback` | High | simulated package transaction with exact versions and confined maintainer scripts |
| `services.restore-configuration` | High | allowlisted manifest restore and service-specific validators |
| `network.restore-configuration` | High | backend detection, local console and timed connectivity rollback |
| `files.recover` | High | immutable source, distinct destination, quotas, hashes and resumable manifest |
| `system.rollback-checkpoint` | High | versioned transactional system-checkpoint provider |
| `user.account-recovery` | Critical | physical-presence-gated PAM/passwd broker and secret-safe audit |

## Public boundary

The common input contract accepts only opaque semantic identifiers:

- session, target resource and target fingerprint;
- server-generated plan and plan fingerprint;
- verified ProtectionCheckpoint ID;
- one-use authorization ID;
- `dry_run`.

Specialized contracts add only semantic fields. File recovery requires an exact distinct
destination and recovery manifest. System rollback requires a source checkpoint and enumerated
protected elements. Account recovery requires an opaque account ID, an allowlisted recovery
method and a physical-presence challenge ID.

All input models use `extra="forbid"`. They reject `command`, `argv`, `shell`, `script`, paths,
mountpoints, devices and any other field not owned by the backend contract. A future root broker
must construct its fixed executable and arguments from the authorized plan; the API, Agent and
LLM never provide them.

## Fail-closed behavior

- Direct execution returns Problem Details `CAPABILITY_DISABLED` before a workflow starts.
- The Reasoning Engine may explain that a capability matches an objective, but returns `stopped`
  with the provider blocker instead of a ready mutation plan.
- AgentRun may show the requested recovery step after its diagnostic prerequisites. The step
  remains `BLOCKED` and is excluded from `AUTORIZO SOLO LECTURA`.
- Every mutation contract declares both `requires_authorization=true` and
  `requires_protection_checkpoint=true`.
- Critical capabilities additionally require a future local physical-presence policy before
  their provider can be enabled.

An installed contract is not execution authority. Enabling one capability requires a dedicated
broker, exact plan/target/checkpoint validation, one-use consent, provider-specific postchecks,
audit evidence and tests proving cleanup and failure behavior.

## Activation checklist

For each capability, a follow-up implementation must:

1. add a dedicated broker verb with a fixed argument builder and deny-by-default policy;
2. create an immutable plan and bind it to exact resource fingerprints;
3. create and verify an adequate checkpoint (not merely reuse a file backup when insufficient);
4. implement dry-run/preflight and show the exact expected changes;
5. request risk-appropriate local authorization after the final plan fingerprint exists;
6. revalidate identity, plan, checkpoint and authorization inside the privileged broker;
7. verify the result independently and record `UNKNOWN` rather than guessing after uncertainty;
8. exercise cancellation, timeout, power-loss and cleanup cases in VM/loop-device tests.

`backend/tests/test_recovery_capabilities.py` verifies discovery, truthful availability,
fail-closed execution, disabled-capability reasoning, read-only/mutation authorization separation
and rejection of shell/path material.
