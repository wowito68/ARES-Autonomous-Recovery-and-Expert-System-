#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: inspect_iso.sh PATH_TO_ISO' >&2
    exit 2
fi

iso=$1
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }
command -v xorriso >/dev/null 2>&1 || { printf '%s\n' 'xorriso is required.' >&2; exit 1; }
command -v veritysetup >/dev/null 2>&1 || { printf '%s\n' 'veritysetup is required.' >&2; exit 1; }
command -v unsquashfs >/dev/null 2>&1 || { printf '%s\n' 'unsquashfs is required.' >&2; exit 1; }
command -v lsinitramfs >/dev/null 2>&1 || { printf '%s\n' 'lsinitramfs is required.' >&2; exit 1; }

iso_dir=$(CDPATH= cd -- "$(dirname -- "${iso}")" && pwd)
iso_name=$(basename -- "${iso}")
sidecar="${iso_dir}/${iso_name}.sha256"
[ -f "${sidecar}" ] || { printf 'SHA-256 sidecar is missing: %s\n' "${sidecar}" >&2; exit 1; }
[ "$(wc -l < "${sidecar}")" -eq 1 ] || { printf '%s\n' 'SHA-256 sidecar must contain exactly one entry.' >&2; exit 1; }
awk -v expected="${iso_name}" '
    $1 !~ /^[0-9a-f]{64}$/ || $2 != expected { exit 1 }
' "${sidecar}" || { printf '%s\n' 'SHA-256 sidecar has an invalid hash or filename.' >&2; exit 1; }
(
    cd "${iso_dir}"
    sha256sum --check "${iso_name}.sha256"
)

el_torito=$(xorriso -indev "${iso}" -report_el_torito plain 2>&1)
system_area=$(xorriso -indev "${iso}" -report_system_area plain 2>&1)
volume_info=$(xorriso -indev "${iso}" -pvd_info 2>&1)

printf '%s\n' "${el_torito}"
printf '%s\n' "${system_area}"

printf '%s' "${el_torito}" | grep -Eqi 'El Torito boot img.*BIOS.*[[:space:]]y[[:space:]]' || { printf '%s\n' 'Bootable BIOS El Torito entry not found.' >&2; exit 1; }
printf '%s' "${el_torito}" | grep -Eqi 'El Torito boot img.*UEFI.*[[:space:]]y[[:space:]]' || { printf '%s\n' 'Bootable UEFI El Torito entry not found.' >&2; exit 1; }
printf '%s' "${system_area}" | grep -Eqi 'System area summary:.*MBR' || { printf '%s\n' 'Hybrid MBR metadata not found.' >&2; exit 1; }
printf '%s' "${system_area}" | grep -Eqi 'System area summary:.*GPT' || { printf '%s\n' 'Hybrid GPT metadata not found.' >&2; exit 1; }
printf '%s' "${volume_info}" \
    | grep -Eq "Volume Id[[:space:]]*:[[:space:]]*'?ARES_OS_AMD64'?[[:space:]]*$" \
    || { printf '%s\n' 'Unexpected ISO volume identifier.' >&2; exit 1; }

live_listing=$(xorriso -indev "${iso}" -ls /live 2>&1)
for payload in filesystem.squashfs filesystem.squashfs.verity filesystem.squashfs.roothash; do
    printf '%s\n' "${live_listing}" | grep -Fq "${payload}" || { printf 'Live payload is missing: %s\n' "${payload}" >&2; exit 1; }
done

temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-inspect.XXXXXX")
trap 'rm -rf -- "${temp_dir}"' EXIT HUP INT TERM
for payload in filesystem.squashfs filesystem.squashfs.verity filesystem.squashfs.roothash initrd.img; do
    xorriso -osirrox on -indev "${iso}" \
        -extract "/live/${payload}" "${temp_dir}/${payload}" >/dev/null 2>&1
done
root_hash=$(tr -d '[:space:]' < "${temp_dir}/filesystem.squashfs.roothash")
printf '%s' "${root_hash}" | grep -Eq '^[0-9a-f]{64}$' || { printf '%s\n' 'Invalid dm-verity root hash.' >&2; exit 1; }
expected_salt=$(sha256sum "${temp_dir}/filesystem.squashfs" | awk '{print $1}')
actual_salt=$(veritysetup dump "${temp_dir}/filesystem.squashfs.verity" \
    | awk -F: '$1 == "Salt" {gsub(/[[:space:]]/, "", $2); print $2}')
[ "${actual_salt}" = "${expected_salt}" ] || {
    printf '%s\n' 'The dm-verity salt is not reproducibly bound to SquashFS.' >&2
    exit 1
}
expected_uuid=$(printf '%s\n' "${expected_salt}" \
    | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/\1-\2-\3-\4-\5/')
actual_uuid=$(veritysetup dump "${temp_dir}/filesystem.squashfs.verity" \
    | awk -F: '$1 == "UUID" {gsub(/[[:space:]]/, "", $2); print $2}')
[ "${actual_uuid}" = "${expected_uuid}" ] || {
    printf '%s\n' 'The dm-verity UUID is not reproducibly bound to SquashFS.' >&2
    exit 1
}
veritysetup verify \
    "${temp_dir}/filesystem.squashfs" \
    "${temp_dir}/filesystem.squashfs.verity" \
    "${root_hash}" >/dev/null

unsquashfs -ll "${temp_dir}/filesystem.squashfs" > "${temp_dir}/squashfs.list"
for runtime_path in \
    opt/ares/backend/src/ares/main.py \
    opt/ares/backend/src/ares/capabilities/discovery.py \
    opt/ares/backend/src/ares/capabilities/manager.py \
    opt/ares/backend/src/ares/capabilities/plugins/disk_analysis.py \
    opt/ares/backend/src/ares/workflows/engine.py \
    opt/ares/backend/src/ares/events/bus.py \
    opt/ares/backend/src/ares/knowledge/graph.py \
    opt/ares/backend/src/ares/planner/engine.py \
    opt/ares/backend/src/ares/reasoning/engine.py \
    usr/lib/ares/ares-boot-integrity \
    usr/lib/ares/ares-hardware-boot \
    usr/lib/ares/ares-kiosk-launch \
    usr/share/ares/platform/index.html \
    usr/share/ares/platform/app.js \
    usr/share/ares/platform/unavailable.html; do
    grep -Fq "squashfs-root/${runtime_path}" "${temp_dir}/squashfs.list" \
        || { printf 'Required runtime path is missing from SquashFS: /%s\n' "${runtime_path}" >&2; exit 1; }
done
grep -Eq 'squashfs-root/opt/ares/backend/src/ares/__pycache__/main\.cpython-[0-9]+\.pyc$' \
    "${temp_dir}/squashfs.list" \
    || { printf '%s\n' 'Precompiled ARES backend bytecode is missing from SquashFS.' >&2; exit 1; }
grep -Eq 'squashfs-root/usr/lib/python3\.[0-9]+/concurrent/futures/__pycache__/thread\.cpython-[0-9]+\.pyc$' \
    "${temp_dir}/squashfs.list" \
    || { printf '%s\n' 'Precompiled Python standard-library bytecode is missing from SquashFS.' >&2; exit 1; }
for forbidden_path in \
    etc/nvme/hostid \
    var/cache/apt/pkgcache.bin \
    var/cache/apt/srcpkgcache.bin; do
    if grep -Fq "squashfs-root/${forbidden_path}" "${temp_dir}/squashfs.list"; then
        printf 'Nondeterministic build state remains in SquashFS: /%s\n' \
            "${forbidden_path}" >&2
        exit 1
    fi
done

unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/systemd/system/ares-backend.service > "${temp_dir}/ares-backend.service"
grep -qx 'Type=simple' "${temp_dir}/ares-backend.service" \
    || { printf '%s\n' 'The ISO contains an invalid ARES backend service type.' >&2; exit 1; }
grep -qx 'PrivateDevices=yes' "${temp_dir}/ares-backend.service" \
    || { printf '%s\n' 'The ISO backend can access host block devices.' >&2; exit 1; }
grep -qx 'Environment=ARES_CAPABILITY_STATE_DIR=/var/lib/ares/capabilities' \
    "${temp_dir}/ares-backend.service" \
    || { printf '%s\n' 'The ISO backend lacks durable capability state.' >&2; exit 1; }
grep -qx 'ReadOnlyPaths=/run/ares/hardware/public' \
    "${temp_dir}/ares-backend.service" \
    || { printf '%s\n' 'The ISO backend hardware evidence path is not read-only.' >&2; exit 1; }
grep -qx 'ReadWritePaths=/var/lib/ares /run/ares/api' \
    "${temp_dir}/ares-backend.service" \
    || { printf '%s\n' 'The ISO backend write paths are not confined.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/tmpfiles.d/ares.conf > "${temp_dir}/ares.tmpfiles"
grep -Fqx 'd /var/lib/ares/capabilities 0700 ares-api ares-api -' \
    "${temp_dir}/ares.tmpfiles" \
    || { printf '%s\n' 'The ISO lacks the protected capability state directory.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/share/ares/platform/app.js > "${temp_dir}/app.js"
grep -Fq '/capabilities/storage.disk-analysis/executions' "${temp_dir}/app.js" \
    || { printf '%s\n' 'The ISO interface does not expose Disk Analysis.' >&2; exit 1; }
grep -Fq '/planner/plan' "${temp_dir}/app.js" \
    || { printf '%s\n' 'The ISO interface does not expose Capability planning.' >&2; exit 1; }
if grep -Eq '(^|[,{[:space:]])command[[:space:]]*:' "${temp_dir}/app.js"; then
    printf '%s\n' 'The ISO interface sends forbidden command input to a capability.' >&2
    exit 1
fi
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/systemd/system/ares-boot-integrity.service \
    > "${temp_dir}/ares-boot-integrity.service"
grep -qx 'TimeoutStartSec=15s' "${temp_dir}/ares-boot-integrity.service" \
    || { printf '%s\n' 'The ISO contains an unbounded boot-integrity service.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/ares/ares-boot-integrity > "${temp_dir}/ares-boot-integrity"
grep -qx '#!/bin/sh' "${temp_dir}/ares-boot-integrity" \
    || { printf '%s\n' 'Boot integrity still depends on Python in the critical path.' >&2; exit 1; }
grep -Fq '/run/live/rootfs/filesystem.squashfs' \
    "${temp_dir}/ares-boot-integrity" \
    || { printf '%s\n' 'The ISO does not verify the effective Live root mount.' >&2; exit 1; }
grep -Fq 'CRYPT-VERITY-' "${temp_dir}/ares-boot-integrity" \
    || { printf '%s\n' 'The ISO does not verify the kernel dm-verity identity.' >&2; exit 1; }
if grep -Eq '(^|[ /])dmsetup([[:space:]]|$)' "${temp_dir}/ares-boot-integrity"; then
    printf '%s\n' 'The ISO boot-integrity service opens device-mapper control.' >&2
    exit 1
fi
grep -qx 'PrivateDevices=yes' "${temp_dir}/ares-boot-integrity.service" \
    || { printf '%s\n' 'The ISO boot-integrity service can access device nodes.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/systemd/system/ares-boot-ready.service \
    > "${temp_dir}/ares-boot-ready.service"
grep -qx 'SupplementaryGroups=ares-api' "${temp_dir}/ares-boot-ready.service" \
    || {
        printf '%s\n' \
            'The ISO boot marker cannot traverse the protected public hardware inventory.' >&2
        exit 1
    }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/systemd/system/ares-hardware.service > "${temp_dir}/ares-hardware.service"
grep -qx 'TimeoutStartSec=30s' "${temp_dir}/ares-hardware.service" \
    || { printf '%s\n' 'The ISO contains an invalid hardware inventory timeout.' >&2; exit 1; }
grep -qx 'ExecStart=/usr/lib/ares/ares-hardware-boot' \
    "${temp_dir}/ares-hardware.service" \
    || { printf '%s\n' 'The ISO does not use the boot-critical hardware phase.' >&2; exit 1; }
grep -q '^CapabilityBoundingSet=.*CAP_DAC_OVERRIDE' \
    "${temp_dir}/ares-hardware.service" \
    || { printf '%s\n' 'The ISO hardware service cannot publish its inventory.' >&2; exit 1; }
grep -qx 'ReadWritePaths=/run/ares/hardware' \
    "${temp_dir}/ares-hardware.service" \
    || { printf '%s\n' 'The ISO hardware write capability is not confined.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/systemd/system/ares-hardware-enrichment.service \
    > "${temp_dir}/ares-hardware-enrichment.service"
grep -qx 'ExecStart=/usr/lib/ares/ares-hardware-inventory enrich' \
    "${temp_dir}/ares-hardware-enrichment.service" \
    || { printf '%s\n' 'The ISO lacks automatic hardware enrichment.' >&2; exit 1; }
grep -q '^CapabilityBoundingSet=.*CAP_DAC_OVERRIDE' \
    "${temp_dir}/ares-hardware-enrichment.service" \
    || { printf '%s\n' 'The ISO enrichment service cannot publish its inventory.' >&2; exit 1; }
grep -qx 'ReadWritePaths=/run/ares/hardware' \
    "${temp_dir}/ares-hardware-enrichment.service" \
    || { printf '%s\n' 'The ISO enrichment write capability is not confined.' >&2; exit 1; }
unsquashfs -cat "${temp_dir}/filesystem.squashfs" \
    usr/lib/ares/ares-hardware-boot > "${temp_dir}/ares-hardware-boot"
grep -Fq 'for device in /sys/block/*' "${temp_dir}/ares-hardware-boot" \
    || { printf '%s\n' 'The ISO boot inventory does not use sysfs storage data.' >&2; exit 1; }
grep -Fq 'for interface in /sys/class/net/*' "${temp_dir}/ares-hardware-boot" \
    || { printf '%s\n' 'The ISO boot inventory does not use sysfs network data.' >&2; exit 1; }
if grep -Eq '(^|[ /])(python3|lscpu|lsblk|lspci|findmnt|ip|iw|smartctl)([[:space:]]|$)' \
    "${temp_dir}/ares-hardware-boot"; then
    printf '%s\n' 'The ISO readiness path still launches heavyweight hardware probes.' >&2
    exit 1
fi

TMPDIR="${temp_dir}" lsinitramfs "${temp_dir}/initrd.img" > "${temp_dir}/initramfs.list"
grep -Eq '(^|/)libcryptsetup\.so\.12$' "${temp_dir}/initramfs.list" \
    || { printf '%s\n' 'The ISO initramfs lacks libcryptsetup.so.12.' >&2; exit 1; }
grep -Eq '(^|/)dm-verity\.ko(\.(gz|xz|zst))?$' "${temp_dir}/initramfs.list" \
    || { printf '%s\n' 'The ISO initramfs lacks the dm-verity kernel module.' >&2; exit 1; }

printf '%s\n' 'ISO boot metadata, root filesystem, initramfs, and dm-verity inspection passed.'
