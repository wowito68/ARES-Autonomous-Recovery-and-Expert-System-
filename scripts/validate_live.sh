#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

for required in \
    Makefile \
    scripts/smoke_test_iso.sh \
    scripts/test_boot_integrity.sh \
    scripts/test_reproducible_verity.sh \
    scripts/veritysetup_reproducible.sh \
    scripts/verify_reproducible.sh \
    live/release.env \
    live/Dockerfile.build \
    live/auto/config \
    live/config/rootfs/excludes \
    live/config/bootloaders/grub-pc/grub.cfg \
    live/config/bootloaders/syslinux_common/live.cfg.in \
    live/config/hooks/live/0400-ares-static-live-config.hook.chroot \
    live/config/hooks/live/0800-ares-python-cache.hook.chroot \
    live/config/hooks/live/0900-ares-cleanup.hook.chroot \
    live/config/hooks/live/0950-ares-update-markers.hook.chroot \
    live/config/hooks/live/0990-ares-final-policy-check.hook.chroot \
    live/config/includes.chroot/etc/initramfs-tools/hooks/ares-verity \
    live/config/includes.chroot/opt/ares/backend/bin/ares-api \
    live/config/includes.chroot/opt/ares/llm/bin/ares-llm \
    live/config/includes.chroot/usr/share/ares/platform/app.js \
    live/config/includes.chroot/usr/share/ares/platform/unavailable.html \
    live/config/includes.chroot/usr/lib/systemd/system/ares.target \
    live/config/includes.chroot/usr/lib/systemd/system/ares-boot-ready.service \
    live/config/includes.chroot/usr/lib/systemd/system/ares-hardware-enrichment.service \
    live/config/includes.chroot/usr/lib/systemd/system/ares-hardware-enrichment.timer \
    live/config/includes.chroot/etc/nftables.conf \
    live/config/includes.chroot/usr/lib/ares/ares-hardware-boot \
    live/config/includes.chroot/usr/lib/ares/ares-hardware-inventory; do
    [ -f "${repo_root}/${required}" ] || fail "missing ${required}"
done

for required in \
    backend/src/ares/capabilities/discovery.py \
    backend/src/ares/capabilities/manager.py \
    backend/src/ares/capabilities/plugins/disk_analysis.py \
    backend/src/ares/events/bus.py \
    backend/src/ares/knowledge/graph.py \
    backend/src/ares/planner/engine.py \
    backend/src/ares/reasoning/engine.py \
    backend/src/ares/workflows/engine.py \
    docs/architecture-v2-capabilities.md \
    docs/architecture-v2-extensibility.md \
    docs/diagrams/ares-v2-capability-flow.mmd \
    docs/diagrams/ares-v2-disk-analysis.mmd; do
    [ -f "${repo_root}/${required}" ] || fail "missing ARES v2 input ${required}"
done

set -a
# shellcheck disable=SC1091
. "${repo_root}/live/release.env"
set +a

[ "${ARES_DEBIAN_CODENAME}" = "trixie" ] || fail 'the Debian codename must be trixie'
printf '%s' "${ARES_BASE_IMAGE}" | grep -Eq '^debian:13\.[0-9]+-slim@sha256:[0-9a-f]{64}$' || fail 'the builder image is not pinned by digest'
printf '%s' "${ARES_DEBIAN_SNAPSHOT}" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' || fail 'invalid Debian snapshot timestamp'
grep -q -- '--firmware-chroot false' "${repo_root}/live/auto/config" || fail 'firmware auto-discovery must remain disabled'
grep -Eq '^[[:space:]]*cryptsetup-bin \\' "${repo_root}/live/Dockerfile.build" || fail 'the builder must provide veritysetup'
grep -q 'policy drop;' "${repo_root}/live/config/includes.chroot/etc/nftables.conf" || fail 'the offline firewall must fail closed'
if grep -q -- '--chroot-squashfs-compression-level' "${repo_root}/live/auto/config"; then
    fail 'the configured XZ compressor does not accept a generic compression-level option'
fi
if grep -Eq 'user-default-groups=[^"[:space:]]*netdev|systemd\.unit=multi-user\.target' "${repo_root}/live/auto/config"; then
    fail 'the live user or safe-graphics entry has an unsafe boot configuration'
fi

find "${repo_root}/scripts" "${repo_root}/live/auto" "${repo_root}/live/config/hooks/live" \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares" \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system-generators" \
    -type f | while IFS= read -r file; do
        first_line=$(sed -n '1p' "${file}")
        case "${first_line}" in
            '#!/bin/sh'*) sh -n "${file}" || exit 1 ;;
            '#!/usr/bin/python3'*)
                python3 - "${file}" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), str(path), "exec")
PY
                ;;
        esac
        case "${first_line}" in
            '#!'*) [ -x "${file}" ] || { printf 'Not executable: %s\n' "${file}" >&2; exit 1; } ;;
        esac
    done

find "${repo_root}/live" \
    \( -path "${repo_root}/live/.cache" -o -path "${repo_root}/live/.build" \) -prune -o \
    -type f -name '*.json' -print | while IFS= read -r file; do
    python3 -m json.tool "${file}" >/dev/null || exit 1
done

python3 - "${repo_root}/live/config/includes.chroot/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

ET.parse(sys.argv[1])
PY

for mode in 'ares.mode=live' 'ares.mode=forensic' 'ares.mode=recovery' 'ares.retention=persistent'; do
    grep -q "${mode}" "${repo_root}/live/config/bootloaders/grub-pc/grub.cfg" || fail "GRUB lacks ${mode}"
    grep -q "${mode}" "${repo_root}/live/config/bootloaders/syslinux_common/live.cfg.in" || fail "SYSLINUX lacks ${mode}"
done

if grep -q '@APPEND_LIVE_FAILSAFE@' "${repo_root}/live/config/bootloaders/grub-pc/grub.cfg"; then
    fail 'GRUB uses a SYSLINUX-only failsafe placeholder'
fi

find "${repo_root}/live/config/package-lists" -type f -name '*.list.chroot' | while IFS= read -r file; do
    LC_ALL=C sort -c -u "${file}" || { printf 'Package list is not sorted and unique: %s\n' "${file}" >&2; exit 1; }
done

if rg -n '^(firmware-b43-installer|firmware-b43legacy-installer)$' \
    "${repo_root}/live/config/package-lists"; then
    fail 'network-downloading firmware installers are forbidden'
fi

if rg -n 'curl[^\n|]*\|[[:space:]]*(sh|bash)|--apt-secure[=[:space:]]+false|--distribution[=[:space:]]+(stable|testing|sid)' \
    "${repo_root}/live" "${repo_root}/scripts"; then
    fail 'a forbidden mutable or unsafe build pattern was found'
fi

if rg -n 'ConditionPathIsExecutable=' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd"; then
    fail 'systemd uses ConditionFileIsExecutable, not ConditionPathIsExecutable'
fi

if rg -n '^(xfce4-panel|xfce4-terminal)$' "${repo_root}/live/config/package-lists"; then
    fail 'the kiosk image must not install an interactive panel or terminal by default'
fi

grep -q '^Exec=/usr/lib/ares/ares-kiosk-launch$' \
    "${repo_root}/live/config/includes.chroot/etc/xdg/autostart/ares-kiosk.desktop" \
    || fail 'the kiosk must inherit the active graphical session environment'
grep -q '^fallback_url=file:///usr/share/ares/platform/unavailable.html$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-kiosk-launch" \
    || fail 'the kiosk fallback must be diagnostic rather than a broken API dashboard'
grep -q '^Type=simple$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-backend.service" \
    || fail 'the Uvicorn backend does not implement sd_notify'
grep -q '^PrivateDevices=yes$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-backend.service" \
    || fail 'the API backend must not receive direct block-device access'
grep -Fq 'd /var/lib/ares/capabilities 0700 ares-api ares-api -' \
    "${repo_root}/live/config/includes.chroot/usr/lib/tmpfiles.d/ares.conf" \
    || fail 'the private Capability journal directory must be created reproducibly'
grep -Fq '/capabilities/storage.disk-analysis/executions' \
    "${repo_root}/live/config/includes.chroot/usr/share/ares/platform/app.js" \
    || fail 'the local interface must expose the Disk Analysis capability'
grep -Fq '/planner/plan' \
    "${repo_root}/live/config/includes.chroot/usr/share/ares/platform/app.js" \
    || fail 'the local interface must expose command-free Capability planning'
if grep -Fq '"command"' \
    "${repo_root}/live/config/includes.chroot/usr/share/ares/platform/app.js"; then
    fail 'the local interface must not send command fields to a capability'
fi
grep -q 'copy_exec.*libcryptsetup' \
    "${repo_root}/live/config/includes.chroot/etc/initramfs-tools/hooks/ares-verity" \
    || fail 'the initramfs must include libmount dm-verity dlopen dependencies'
grep -Fq 'api=%s ui=%s hardware=%s' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-ready" \
    || fail 'the boot marker must prove backend, interface, and hardware readiness'
grep -qx 'SupplementaryGroups=ares-api' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-boot-ready.service" \
    || fail 'the capability-free boot marker must be able to traverse the ares-api hardware view'
grep -q -- '--invalidation-mode checked-hash' \
    "${repo_root}/live/config/hooks/live/0800-ares-python-cache.hook.chroot" \
    || fail 'the backend must have reproducible precompiled bytecode'
grep -Fq 'sysconfig.get_path("stdlib")' \
    "${repo_root}/live/config/hooks/live/0800-ares-python-cache.hook.chroot" \
    || fail 'the hardware inventory must not compile the Python standard library at boot'
grep -q '^TimeoutStartSec=30s$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-hardware.service" \
    || fail 'the hardware inventory must retain a bounded slow-hardware margin'
grep -q '^TimeoutStartSec=15s$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-boot-integrity.service" \
    || fail 'boot integrity must have a bounded critical-path timeout'
grep -q '^#!/bin/sh$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-integrity" \
    || fail 'boot integrity must not wait for a Python interpreter on the critical path'
grep -Fq '/run/live/rootfs/filesystem.squashfs' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-integrity" \
    || fail 'boot integrity must verify the effective Live root mount'
grep -Fq 'CRYPT-VERITY-' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-integrity" \
    || fail 'boot integrity must verify the kernel dm-verity identity'
if grep -Eq '(^|[ /])dmsetup([[:space:]]|$)' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-integrity"; then
    fail 'boot integrity must not open the device-mapper control path'
fi
grep -qx 'PrivateDevices=yes' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-boot-integrity.service" \
    || fail 'boot integrity must not access device nodes'
grep -Fq 'Another ARES ISO build is already running' \
    "${repo_root}/scripts/build_iso.sh" \
    || fail 'ISO builds must be serialized to prevent concurrent resource exhaustion'
grep -Fq 'MKSQUASHFS_OPTIONS=-processors ${squashfs_processors} -mem ${squashfs_memory}' \
    "${repo_root}/scripts/build_iso.sh" \
    || fail 'SquashFS compression must have explicit CPU and memory limits'
grep -Fq 'PATH="/build/local/bin:${PATH}"' \
    "${repo_root}/scripts/build_live_in_container.sh" \
    || fail 'the deterministic veritysetup wrapper must precede the builder tool'
grep -Fq 'salt=$(sha256sum "${data_device}"' \
    "${repo_root}/scripts/veritysetup_reproducible.sh" \
    || fail 'dm-verity salt must be derived from the immutable rootfs digest'
grep -Fq -- '--uuid "${uuid}"' \
    "${repo_root}/scripts/veritysetup_reproducible.sh" \
    || fail 'dm-verity UUID must be derived from the immutable rootfs digest'
grep -Fq 'actual_salt=$(veritysetup dump' \
    "${repo_root}/scripts/inspect_iso.sh" \
    || fail 'the ISO inspector must verify the deterministic dm-verity salt'
grep -Fq 'actual_uuid=$(veritysetup dump' \
    "${repo_root}/scripts/inspect_iso.sh" \
    || fail 'the ISO inspector must verify the deterministic dm-verity UUID'
grep -Fq 'accel=${ARES_QEMU_ACCEL:-tcg,thread=multi}' \
    "${repo_root}/scripts/smoke_test_iso.sh" \
    || fail 'the guest smoke test must default to reproducible TCG acceleration'
[ "$(grep -Fc 'snapshot=on' "${repo_root}/scripts/smoke_test_iso.sh")" -eq 2 ] \
    || fail 'both hybrid USB smoke tests must protect the ISO with ephemeral snapshots'
grep -Fq 'ARES_REPRODUCIBLE_COLD_CACHE' \
    "${repo_root}/scripts/verify_reproducible.sh" \
    || fail 'the reproducibility gate must retain an explicit cold-cache release mode'
grep -Fq 'ARES_KEEP_REPRO_EVIDENCE' \
    "${repo_root}/scripts/verify_reproducible.sh" \
    || fail 'the reproducibility gate must support retained failure evidence'
grep -Fq '/etc/nvme/hostid' \
    "${repo_root}/live/config/hooks/live/0900-ares-cleanup.hook.chroot" \
    || fail 'the random package-generated NVMe host ID must not enter the Live root'
grep -Fq '/var/cache/apt/*cache.bin' \
    "${repo_root}/live/config/hooks/live/0900-ares-cleanup.hook.chroot" \
    || fail 'nondeterministic APT binary caches must not enter the Live root'
grep -Fqx 'etc/nvme/hostid' \
    "${repo_root}/live/config/rootfs/excludes" \
    || fail 'SquashFS must exclude package-generated NVMe host IDs'
grep -Fqx 'var/cache/apt/*cache.bin' \
    "${repo_root}/live/config/rootfs/excludes" \
    || fail 'SquashFS must exclude APT caches regenerated by live-build'
grep -Fq 'A build-specific NVMe host identifier remains' \
    "${repo_root}/live/config/hooks/live/0990-ares-final-policy-check.hook.chroot" \
    || fail 'the final policy hook must reject build-specific NVMe host IDs'
grep -Fq 'A nondeterministic APT binary cache remains' \
    "${repo_root}/live/config/hooks/live/0990-ares-final-policy-check.hook.chroot" \
    || fail 'the final policy hook must reject APT binary caches'
grep -q '^ExecStart=/usr/lib/ares/ares-hardware-boot$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-hardware.service" \
    || fail 'platform readiness must wait only for the boot-critical inventory'
grep -q '^ExecStart=/usr/lib/ares/ares-hardware-inventory enrich$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-hardware-enrichment.service" \
    || fail 'the complete hardware inventory must be enriched automatically'
for unit in ares-hardware.service ares-hardware-enrichment.service; do
    grep -q '^CapabilityBoundingSet=.*CAP_DAC_OVERRIDE' \
        "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/${unit}" \
        || fail "${unit} cannot publish into the UID 972-owned runtime directory"
    grep -q '^ReadWritePaths=/run/ares/hardware$' \
        "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/${unit}" \
        || fail "${unit} must confine its write capability to the hardware runtime"
done
grep -Fq 'for device in /sys/block/*' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-hardware-boot" \
    || fail 'the boot-critical storage inventory must use sysfs'
grep -Fq 'for interface in /sys/class/net/*' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-hardware-boot" \
    || fail 'the boot-critical network inventory must use sysfs'
if grep -Eq '(^|[ /])(python3|lscpu|lsblk|lspci|findmnt|ip|iw|smartctl)([[:space:]]|$)' \
    "${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-hardware-boot"; then
    fail 'the readiness path must not launch Python or heavyweight hardware probes'
fi
grep -Fq '/var/lib/live/config/locales' \
    "${repo_root}/live/config/hooks/live/0400-ares-static-live-config.hook.chroot" \
    || fail 'immutable locale configuration must be completed at build time'
grep -Fq 'live-config.nocomponents username=ares' \
    "${repo_root}/live/auto/config" \
    || fail 'the immutable Live account must not be rebuilt during boot'
grep -Fq 'u ares 1000:1000' \
    "${repo_root}/live/config/includes.chroot/usr/lib/sysusers.d/ares.conf" \
    || fail 'the Live operator must be created reproducibly at build time'
grep -q '^ConditionKernelCommandLine=ares.mode=forensic$' \
    "${repo_root}/live/config/includes.chroot/usr/lib/systemd/system/ares-block-readonly@.service" \
    || fail 'forensic block units must be skipped outside forensic mode'

"${repo_root}/scripts/test_boot_integrity.sh"
"${repo_root}/scripts/test_reproducible_verity.sh"

printf '%s\n' 'ARES OS source validation passed.'
