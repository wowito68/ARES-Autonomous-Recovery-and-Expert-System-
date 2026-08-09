# Backup & Snapshot Manager — initial backup capabilities

## Scope

This increment implements the offline local-backup subset of the future Backup & Snapshot Manager:

- `backup.create@1.0.0`
- `backup.verify@1.0.0`
- `backup.list@1.0.0`

It does **not** implement filesystem snapshots, restore automation, incremental or differential copies, remote/cloud backup, mandatory encryption, deduplication, advanced compression, or scheduling.

`backup.create` is a `CHANGE` / `MUTATING` Capability with `risk=medium`. `backup.verify` and `backup.list` are read-only observations.

## Plan before mutation

Every new backup starts with a structured `BackupPlan`. The plan is produced by the Backup Tool Layer, not by the LLM. It contains:

- canonical source path and filesystem/device identity;
- canonical destination root and final ARES backup path;
- destination kind (`local_filesystem`, `mounted_external_disk`, or `usb_storage` when the kernel exposes it as removable);
- estimated bytes and file/directory counts;
- explicit exclusions and reasons;
- required and currently available space;
- overwrite policy (`false`);
- checksum policy (`sha256`);
- risk (`medium`);
- authorization requirement;
- immutable SHA-256 fingerprint and expiration.

ARES re-scans and revalidates the plan immediately before authorization and again in the broker before mutation. A changed source, changed device identity, disconnected destination, reduced free space, pre-existing target, or changed plan stops execution.

ETA is not invented. It is `null`/unknown until measured copy throughput makes an estimate possible.

## Source and destination safety

The initial implementation accepts local paths only. Paths must be absolute and existing. It rejects NULs, `..` traversal, symlink components in requested roots, a destination inside the source, and dangerous destination filesystems such as `proc`, `sysfs`, `tmpfs`, `overlay`, `squashfs`, and `iso9660`.

The Tool Layer records both filesystem identity (`major:minor` from mount information) and, where Linux sysfs exposes it, the underlying physical block-device identity. It rejects:

- the same filesystem/partition;
- the same underlying physical block device;
- read-only destinations;
- unwritable destinations;
- incompatible pseudo/read-only filesystems;
- insufficient free space;
- an already existing final or partial ARES backup path.

ARES never follows source symlinks for this format. Symlinks, special files, policy-excluded names and cross-filesystem descendants are represented as structured exclusions. Hidden files, empty files, Unicode filenames and normal long filenames are valid.

## Mutation boundary and authorization

FastAPI does not perform the copy in production. The API/backend sends a semantic `BackupPlan` to `ares-tool-broker` over a Unix socket. The broker accepts no shell string, executable name, command line, or user-controlled `argv`.

The broker:

1. revalidates the exact plan;
2. durably records the mutation intent through `ares-audit-writer`;
3. asks the independent `ares-consent-agent` to create a challenge;
4. waits for an operator decision from the trusted local consent channel;
5. revalidates the plan again;
6. issues a short-lived, one-use grant bound to plan ID, plan fingerprint and session ID;
7. consumes that grant before copying;
8. writes through the Backup Tool Layer;
9. durably records completion/failure.

The web API cannot approve its own mutation. `ares backup create` may request authorization, but approval is performed separately with:

```text
ares consent approve <challenge-id>
```

The consent command displays the exact source, destination, estimated/required/available bytes, file count, exclusions, risk, overwrite policy, verification method, expiration and plan fingerprint. Approval requires typing the exact fingerprint-derived phrase shown by the consent service.

## Transactional copy format

A backup is staged under:

```text
<destination>/ARES/.partial-<backup-id>/
```

Files are created with no-follow/exclusive semantics. Data is SHA-256 hashed while copied, the file descriptor is fsynced, and source inode/device/size/mtime are checked to detect relevant source changes during copy. After all entries are complete, ARES writes `.ares-manifest.json`, fsyncs it and atomically renames the partial directory to:

```text
<destination>/ARES/<backup-id>/
```

Cancellation or copy failure removes the unpublished partial directory. A published backup is never silently overwritten or deleted as workflow compensation.

## Manifest

`BackupManifest` format `1.0` records:

- backup ID;
- source metadata;
- relative path;
- file/directory type;
- size;
- nanosecond mtime;
- permission mode where relevant;
- per-file SHA-256;
- file/directory counts;
- total bytes;
- manifest SHA-256.

A private authoritative copy is stored in ARES state and the same manifest is placed inside the backup. Verification requires those representations to agree.

## Verification

Creation and verification are separate states. `backup.verify` checks:

1. backup root exists and is not a symlink;
2. authoritative manifest checksum;
3. destination `.ares-manifest.json` equality/checksum;
4. expected entries;
5. file count and total size;
6. SHA-256 for every file;
7. missing and unexpected paths;
8. source comparison when the original file still exists.

A corrupt/missing/unexpected destination entry produces `CORRUPTED`. A backup that is internally consistent but whose source later differs produces a failed source-comparison result rather than claiming the backup bytes are corrupt.

Canonical `Backup.status` values are exactly:

`PLANNED`, `VALIDATING`, `RUNNING`, `VERIFYING`, `COMPLETED`, `FAILED`, `CANCELLED`, `CORRUPTED`.

## Progress

The copy publishes structured progress after bounded chunks and completed entries:

- files completed / total;
- bytes completed / total;
- percent;
- measured bytes/second when available;
- ETA only when measured speed is available; otherwise unknown.

No ETA is generated by the LLM.

## Knowledge Graph

Verified backups project facts using:

```text
BackupSource --backed_up_to--> Backup
Backup       --stored_on-----> BackupDestination
Backup       --contains------> BackupEntry
Backup       --verified_by---> BackupVerification
Backup       --protects------> BackupSource
```

The full manifest is authoritative. To bound graph growth, at most 1,024 individual `BackupEntry` nodes are projected per backup; the `Backup` node records whether entry projection was truncated. This is a graph scalability limit, not a backup-data limit.

## Events and audit

The Event Bus emits dotted canonical events including:

- `backup.planned`
- `backup.authorization.requested`
- `backup.started`
- `backup.progress`
- `backup.entry.created`
- `backup.verification.started`
- `backup.verified`
- `backup.completed`
- `backup.failed`
- `backup.cancelled`

Entry events use a path token rather than the relative path to avoid unnecessary disclosure.

Mutation authorization and broker-side write intent/completion are additionally written by the independent Audit Ledger. Its HMAC chain detects modification/reordering with a locally protected key and acknowledges only after durable sync. See ADR-0009 for its trust boundary and remaining rollback-anchor limitation.

## Restore guidance

Automatic restore is intentionally not implemented. The backup is a plain directory tree plus `.ares-manifest.json`. A future `backup.restore` Capability must consume this manifest, generate a restore plan, require its own authorization, validate the target, preserve recovery evidence and re-verify restored data. Users should not interpret the current absence of a restore Capability as permission for ARES to copy data back automatically.

## ProtectionCheckpoint

The Capability SDK now supports `requires_protection_checkpoint`. A future destructive Capability may declare it and the `CapabilityManager` will reject execution unless it receives a `ProtectionCheckpoint` that is:

- `READY`;
- tied to at least one protected resource;
- backed by a verification ID;
- in the same session as the requested mutation when the payload carries a session ID.

No destructive Capability is enabled by this increment. Creating a `ProtectionCheckpoint` automatically from backup/snapshot policy is a later orchestration increment.
