# Read-only diagnostic analysis

Status: implemented as `ares.diagnostic-analysis@1.0.0`.

The plugin adds four diagnostic capabilities and composes them with the existing
`boot.diagnose` capability:

| Capability | Scope | Explicitly excluded |
| --- | --- | --- |
| `storage.space-analysis` | Capacity, inodes, bounded metadata walks and conservative reclaim estimates | Deletion, file-content reads, cross-filesystem traversal |
| `system.memory-analysis` | Runtime `/proc` metrics or clearly labelled historical offline evidence | `drop_caches`, process termination, swap or sysctl changes |
| `packages.health-check` | Local Debian/Ubuntu dpkg/APT metadata | Network, package scripts, install/remove/configure operations |
| `services.failure-analysis` | Fixed `systemctl --failed` probe or bounded persistent logs | Restart, enable, disable, mask or configuration changes |
| `boot.diagnose` | Existing storage snapshot, firmware and visible boot evidence | GRUB/initramfs/NVRAM repair |

## Public input boundary

Inputs contain only `snapshot_id`, `target_resource_id` and, for the space analysis,
`analysis_depth`. Pydantic rejects extra fields, including `command`, `argv`, `shell`,
`script`, `path` and `mountpoint`.

Targets are resolved from the storage snapshot. A unique installed system can be selected
automatically; the AgentRun UI uses the human resource catalog when selection is ambiguous.
The collector never accepts a mountpoint or device path from the model or browser.

Installed targets must already be visible below ARES-managed or recovery mount roots. This
increment does not add a mount or chroot mechanism. A missing or unsafe mount fails closed
instead of silently switching targets.

## Runtime versus offline evidence

Every result contains a target fingerprint and one of these scopes:

- `ares_live_runtime`
- `active_system_runtime`
- `installed_system_offline`

Memory and service collectors never present offline logs as current runtime state. Package
health explicitly reports unsupported targets when a dpkg database is absent.

## Process allowlist

Most collection uses bounded Python metadata readers. The only new subprocess invocation is
the exact passive runtime call:

```text
systemctl --failed --no-legend --plain --no-pager
```

`ReadOnlyDiagnosticProcessRunner` rejects every other executable and every flag variation.
The model cannot access this runner.

## AgentRun integration

The orchestrator maps diagnostic objectives to typed capabilities, always obtains a storage
snapshot first and binds all proposed steps to one short-lived read-only authorization. The
authorization records both step IDs and capability IDs and remains protected by the existing
plan and resource fingerprints.

A combined objective may authorize the five diagnostics together with
`storage.disk-analysis`. A failure in storage evidence blocks dependent diagnostics. A
diagnostic authorization never enables a corrective capability.

## Verification

`backend/tests/test_diagnostic_capabilities.py` covers:

- discovery and real execution of all five diagnostics;
- automatic target resolution;
- runtime/offline scope separation;
- one authorization for a combined plan;
- rejection of command, argv and path material;
- exact process allowlisting;
- untrusted log text remaining data rather than an instruction.
