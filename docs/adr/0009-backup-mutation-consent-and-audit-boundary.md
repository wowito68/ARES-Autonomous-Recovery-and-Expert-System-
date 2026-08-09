# ADR-0009: Backup mutation, consent and audit boundary

- Status: Accepted
- Date: 2026-08-08
- Extends: ADR-0002, ADR-0003, ADR-0006, ADR-0008

## Context

ARES now needs its first production-grade Capability that writes user data: `backup.create`. A backup is intentionally protective, but it is still a mutation: it consumes capacity, creates files, can target the wrong device, can overwrite recovery options if placed on the source storage, and can expose data if authorization is weak.

The existing architecture already establishes four constraints:

1. privileged/device mutation belongs behind `ares-tool-broker` rather than FastAPI;
2. the component requesting a mutation must not be the authority that grants consent;
3. mutation intent/results require a durable audit boundary outside FastAPI;
4. passive storage observation does not grant mutation authority.

The backup increment also needs to become the foundation for future pre-mutation protection checkpoints.

## Decision

### Semantic plan, not command execution

The backend and Capability Workflow exchange a typed `BackupPlan`. Production code never sends a shell string, executable, or caller-controlled `argv` to the broker. The broker exposes only semantic backup actions.

### Exact plan fingerprint and revalidation

The plan contains source/destination storage identities, size/count estimates, exclusions, space requirements, overwrite=false, SHA-256 verification policy and expiration. A canonical SHA-256 fingerprint binds those fields.

The source and destination are revalidated before consent and immediately before the write. A changed source, storage identity, free-space condition or existing destination invalidates the operation.

### Independent consent

`ares-consent-agent` owns the authorization challenge. The broker can create/wait for a challenge but cannot approve one. Approval is accepted only from the trusted local operator UID through the consent socket and requires a fingerprint-derived exact phrase.

A successful decision yields a short-lived one-use broker grant bound to:

- challenge ID;
- plan ID;
- plan fingerprint;
- original session ID.

The web/API request to create a backup is therefore only an authorization request. It is not approval.

### Privileged broker

`ares-tool-broker` remains the only production component allowed to perform the write. It runs as root because future storage capabilities may require device-aware privileges, but its systemd sandbox restricts writable paths and denies network access. The backend user receives only broker-socket access, not direct filesystem privilege expansion.

The broker consumes a valid grant once and executes only the fixed Backup Tool Layer implementation. No destructive disk commands are introduced.

### Durable audit

`ares-audit-writer` is a single-writer local service. Before side effects, the broker requires a durable audit acknowledgement. Records are chained with HMAC-SHA-256 and sequence/previous-MAC fields and are acknowledged only after `fdatasync`/`fsync`.

If the audit writer is unavailable before mutation, the mutation fails closed. If the mutation completed but the final audit acknowledgement cannot be obtained, the broker writes a restricted emergency reconciliation record and reports `BACKUP_RECONCILIATION_REQUIRED` instead of pretending the operation completed normally.

### Transactional backup publication

Copying occurs in a private `.partial-<backup-id>` directory. The final backup path never pre-exists and overwrite is not supported. A completed manifest is written and synced before an atomic rename publishes the backup. Cancellation/failure removes the unpublished partial tree.

### ProtectionCheckpoint gate

Capability metadata may now declare `requires_protection_checkpoint=true`. The `CapabilityManager` rejects such a request unless a same-session, `READY`, verified `ProtectionCheckpoint` is supplied.

This ADR only introduces the policy gate and data contract. No destructive Capability is enabled here, and no automatic checkpoint orchestration is claimed.

## Consequences

Positive:

- FastAPI does not obtain arbitrary destination-write authority in production.
- authorization is bound to the exact reviewed plan rather than a generic “yes”;
- mutation intent is auditable before side effects;
- backup publication is fail-safe against partial success being mistaken for a complete backup;
- future destructive capabilities have a concrete protection prerequisite in the Capability SDK.

Costs:

- production requires three local runtime services: broker, consent and audit writer;
- the initial copy implementation is native Python and intentionally conservative rather than optimized like rsync;
- large manifests and per-file verification can be expensive; this is accepted for verifiability in format 1.0;
- a local HMAC ledger can detect record modification/reordering, but if an attacker can roll back both the ledger and its local key/state together, there is no external monotonic anchor. A TPM/remote anchor is outside this offline increment and remains technical debt.

## Rejected alternatives

### Run copy code directly inside FastAPI

Rejected because it would collapse ADR-0002’s privilege boundary and make the network-facing process the writer.

### Let the API or frontend approve its own backup

Rejected because it violates ADR-0003 and weakens plan-bound consent.

### Use `rsync`/`cp` through arbitrary shell arguments

Rejected. Those tools may become internal broker implementations later, but a Capability must never expose arbitrary command construction as its contract.

### Treat successful copy as successful backup

Rejected. `COMPLETED` requires verification; copy and integrity verification remain distinct.

### Permit same physical disk when partitions differ

Rejected for the default recovery policy. A failure of that physical device could destroy both source and backup. The current policy fails closed when both physical identities are known and equal.

## Follow-up

Recommended next architecture increment: make `ProtectionCheckpoint` a first-class orchestration service capable of selecting between verified backup and future read-only filesystem snapshots, then integrate broker-backed privileged storage identity/SMART evidence. Automatic restore and destructive operations remain separate future capabilities with their own plans and consent.
