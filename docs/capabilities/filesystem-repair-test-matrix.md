# Filesystem Repair — acceptance test matrix

All destructive test cases use temporary regular filesystem image files. Automated tests never select or write a physical block device.

| Requirement | Automated evidence |
| --- | --- |
| healthy filesystem | adapter/Tool Layer structured HEALTHY check |
| corrupt filesystem | synthetic runner plus real ext4 image corruption |
| mounted filesystem | MountSafetyChecker mountinfo fixture |
| busy filesystem | bounded `/proc` cwd/root/exe/fd fixture |
| swap target | `/proc/swaps` fixture |
| bind/nested mounts | synthetic mountinfo fixtures |
| filesystem detection | blkid structured fixture and real ext4 image |
| exact device identity | fingerprint + major/minor/file identity tests |
| UUID/device identity changed | revalidation failure before mutation |
| device disappeared | explicit `FILESYSTEM_DEVICE_DISAPPEARED` path |
| path traversal | production `/dev` namespace validation |
| tool unavailable | structured ToolRequirement / blocked inspection |
| insufficient target access | writable preflight and Problem Details paths |
| dry run | fixed adapter check invocations (`-n`/`--readonly`) |
| unsafe adapter flags | no ext `-y`, no XFS `-L`, no Btrfs `--repair` |
| checkpoint absent | blocked Repair Plan and API 409 |
| partial backup | ProtectionCheckpoint promotion rejected |
| wrong backup device | ProtectionCheckpoint promotion rejected |
| fabricated checkpoint | broker durable-store equality check |
| tampered plan | independently recomputed plan fingerprint rejected |
| explicit authorization | exact filesystem-mutation phrase required |
| authorization expired | expiring grant rejected |
| authorization reuse | one-use broker grant rejected |
| wrong peer identity | broker peer UID rejected |
| repair failure | structured unresolved-repair limitations |
| verification failure | FAILED/PARTIAL/UNKNOWN; never inferred success |
| remount safety | unsafe remount preconditions block execution |
| cancellation before mutation | pending authorization task can be cancelled |
| cancellation during mutation | `FILESYSTEM_CANCELLATION_UNSAFE` |
| process/power interruption | nonterminal durable record reconciles to ABORTED |
| Audit Ledger | mutation intent precedes repair command and results are recorded |
| Knowledge Graph | before/status, checkpoint, repair, verification, after relationships |
| API | inspect/plan/start/status/verification/cancel Problem Details |
| CLI | inspect, repair plan, repair plan-id, repair status parser/service surface |
| Agent | reasoning can select capability but execution schema has no device/command/argv |
| frontend | plan must be visible and executable before request; no web approval |
| real repair | temporary ext4 image: `mkfs.ext4` → controlled `debugfs` corruption → read-only e2fsck detects → preen repair → read-only verification returns healthy |

The real ext4 test is skipped only if the GitHub runner lacks its declared fixture tools (`mkfs.ext4`, `debugfs`, `e2fsck`, `blkid`). It does not use loop devices and does not access `/dev`.
