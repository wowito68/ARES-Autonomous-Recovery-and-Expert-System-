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
    live/release.env \
    live/Dockerfile.build \
    live/auto/config \
    live/config/bootloaders/grub-pc/grub.cfg \
    live/config/bootloaders/syslinux_common/live.cfg.in \
    live/config/hooks/live/0990-ares-final-policy-check.hook.chroot \
    live/config/includes.chroot/usr/lib/systemd/system/ares.target \
    live/config/includes.chroot/usr/lib/systemd/system/ares-boot-ready.service \
    live/config/includes.chroot/etc/nftables.conf \
    live/config/includes.chroot/usr/lib/ares/ares-hardware-inventory; do
    [ -f "${repo_root}/${required}" ] || fail "missing ${required}"
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

printf '%s\n' 'ARES OS source validation passed.'
