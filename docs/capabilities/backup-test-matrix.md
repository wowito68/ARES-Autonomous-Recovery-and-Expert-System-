# Backup validation matrix

The Backup & Snapshot Manager increment is validated without user data or real storage mutation. Automated tests use temporary directories, synthetic `mountinfo`, local Unix sockets, the test executor, and in-memory audit where appropriate.

Covered scenarios include:

- valid source and destination;
- missing source and missing destination;
- same filesystem/partition;
- same physical-device identity;
- insufficient destination space;
- read/write permission failures;
- destination inside source;
- unsafe symlink roots and source symlink exclusions;
- hidden, empty, Unicode and long normal filenames;
- multi-megabyte files with chunk progress;
- policy exclusions and cross-filesystem/special-file behavior;
- disconnected destination during revalidation;
- incompatible destination filesystem;
- absence of external `rsync`/`cp`/`find`/`sha256sum` binaries (the current Tool Layer does not depend on them);
- plan fingerprint and source-change revalidation;
- transactional partial-copy cleanup;
- cancellation;
- broker timeout/unavailability;
- independent consent, exact phrase and peer-UID separation;
- one-use authorization grants;
- durable HMAC audit chaining and tamper detection;
- backup manifest generation and destination-manifest validation;
- SHA-256 corruption, missing/unexpected entries and source-changed distinction;
- startup reconciliation of interrupted records;
- Knowledge Graph projection and required backup relations;
- Event Bus / terminal state behavior;
- API plan/create/list/get/manifest/verification/verify/cancel;
- CLI command surface and shared service usage;
- `ProtectionCheckpoint` policy gate;
- Python 3.12 and 3.13 CI suites;
- Ruff formatting/lint, strict mypy, package build and frontend JavaScript syntax.

No test reads or writes actual user files or raw block devices.
