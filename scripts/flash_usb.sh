#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/flash_usb.sh --device /dev/sdX [--iso iso/ARES.iso] [--yes]

Writes the ARES hybrid ISO to an entire USB disk.

This is destructive for the selected device. The script refuses partitions,
read-only devices, non-removable/non-USB disks, devices containing the running
root filesystem, and devices smaller than the ISO.
EOF
}

device=
iso=
assume_yes=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --device)
            [ "$#" -ge 2 ] || { printf '%s\n' '--device requires a value.' >&2; exit 2; }
            device=$2
            shift
            ;;
        --iso)
            [ "$#" -ge 2 ] || { printf '%s\n' '--iso requires a value.' >&2; exit 2; }
            iso=$2
            shift
            ;;
        --yes)
            assume_yes=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

[ -n "${device}" ] || { printf '%s\n' 'Missing --device /dev/sdX.' >&2; usage >&2; exit 2; }
if [ -z "${iso}" ]; then
    iso="${repo_root}/iso/ARES.iso"
fi

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Root privileges are required to write a bootable image to a block device.' >&2
    printf 'Run: sudo %s --device %s --iso %s --yes\n' "$0" "${device}" "${iso}" >&2
    exit 1
fi

command -v findmnt >/dev/null 2>&1 || { printf '%s\n' 'findmnt is required.' >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { printf '%s\n' 'sha256sum is required.' >&2; exit 1; }
command -v dd >/dev/null 2>&1 || { printf '%s\n' 'dd is required.' >&2; exit 1; }
command -v cmp >/dev/null 2>&1 || { printf '%s\n' 'cmp is required.' >&2; exit 1; }
command -v blockdev >/dev/null 2>&1 || { printf '%s\n' 'blockdev is required.' >&2; exit 1; }

case "${device}" in
    /dev/*) ;;
    *) printf 'Device must be an absolute /dev path: %s\n' "${device}" >&2; exit 2 ;;
esac
[ -b "${device}" ] || { printf 'Block device not found: %s\n' "${device}" >&2; exit 1; }
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }

device=$(readlink -f -- "${device}")
iso=$(readlink -f -- "${iso}")
iso_dir=$(CDPATH= cd -- "$(dirname -- "${iso}")" && pwd)
iso_name=$(basename -- "${iso}")
sidecar="${iso}.sha256"
[ -f "${sidecar}" ] || { printf 'SHA-256 sidecar is missing: %s\n' "${sidecar}" >&2; exit 1; }

dev_name=$(basename -- "${device}")
sys_block="/sys/block/${dev_name}"
[ -d "${sys_block}" ] || { printf 'Refusing to write a non-disk target: %s\n' "${device}" >&2; exit 1; }

dev_ro=$(cat "${sys_block}/ro")
[ "${dev_ro}" = "0" ] || { printf 'Refusing to write a read-only device: %s\n' "${device}" >&2; exit 1; }

dev_rm=$(cat "${sys_block}/removable")
dev_path=$(readlink -f -- "${sys_block}/device" 2>/dev/null || printf '')
case "${dev_path}" in
    */usb*) dev_usb=1 ;;
    *) dev_usb=0 ;;
esac
if [ "${dev_rm}" != "1" ] && [ "${dev_usb}" != "1" ]; then
    printf 'Refusing to write a device that is not removable/USB: %s (RM=%s USB=%s)\n' \
        "${device}" "${dev_rm}" "${dev_usb}" >&2
    exit 1
fi

root_source=$(findmnt -no SOURCE / || true)
root_parent=
if [ -n "${root_source}" ] && [ -b "${root_source}" ]; then
    root_name=$(basename -- "$(readlink -f -- "${root_source}")")
    if [ -e "/sys/class/block/${root_name}/partition" ]; then
        root_parent_name=$(basename -- "$(readlink -f -- "/sys/class/block/${root_name}/..")")
        root_parent="/dev/${root_parent_name}"
    else
        root_parent=$(readlink -f -- "${root_source}")
    fi
fi
if [ -n "${root_parent}" ] && [ "$(readlink -f -- "${root_parent}")" = "${device}" ]; then
    printf 'Refusing to write the disk that contains the running root filesystem: %s\n' "${device}" >&2
    exit 1
fi

iso_size=$(stat -c%s -- "${iso}")
dev_size=$(blockdev --getsize64 "${device}")
if [ "${iso_size}" -gt "${dev_size}" ]; then
    printf 'ISO is larger than target device: ISO=%s bytes device=%s bytes\n' "${iso_size}" "${dev_size}" >&2
    exit 1
fi

(
    cd "${iso_dir}"
    sha256sum --check "${iso_name}.sha256"
)

printf '%s\n' 'Target device:'
dev_model=$(tr -d '\n' < "${sys_block}/device/model" 2>/dev/null || printf unknown)
dev_serial=$(tr -d '\n' < "${sys_block}/device/serial" 2>/dev/null || printf unknown)
printf '  path:   %s\n' "${device}"
printf '  size:   %s bytes\n' "${dev_size}"
printf '  rm:     %s\n' "${dev_rm}"
printf '  usb:    %s\n' "${dev_usb}"
printf '  model:  %s\n' "${dev_model}"
printf '  serial: %s\n' "${dev_serial}"
printf '%s\n' 'Current mounts on target:'
found_mount=0
for sys_child in "${sys_block}/${dev_name}"*; do
    [ -e "${sys_child}" ] || continue
    child="/dev/$(basename -- "${sys_child}")"
    findmnt -rn -S "${child}" -o SOURCE,TARGET,FSTYPE,OPTIONS && found_mount=1 || true
done
[ "${found_mount}" = "1" ] || printf '  none\n'
printf '\nISO: %s (%s bytes)\n' "${iso}" "${iso_size}"
printf 'This will destroy all data on %s.\n' "${device}"

if [ "${assume_yes}" != "1" ]; then
    printf 'Type exactly "FLASH %s" to continue: ' "${device}"
    IFS= read -r confirmation
    [ "${confirmation}" = "FLASH ${device}" ] || { printf '%s\n' 'Aborted.' >&2; exit 1; }
fi

for sys_child in "${sys_block}/${dev_name}"*; do
    [ -e "${sys_child}" ] || continue
    child="/dev/$(basename -- "${sys_child}")"
    while read -r mountpoint; do
        [ -n "${mountpoint}" ] || continue
        printf 'Unmounting %s from %s...\n' "${child}" "${mountpoint}"
        umount "${mountpoint}"
    done <<EOF
$(findmnt -rn -S "${child}" -o TARGET || true)
EOF
done

printf 'Writing %s to %s...\n' "${iso}" "${device}"
dd if="${iso}" of="${device}" bs=16M status=progress conv=fsync
sync

blockdev --rereadpt "${device}" 2>/dev/null || true
udevadm settle 2>/dev/null || true

printf 'Verifying written bytes...\n'
cmp -n "${iso_size}" "${iso}" "${device}"

printf '%s\n' 'ARES USB image written and verified successfully.'
if command -v lsblk >/dev/null 2>&1; then
    timeout 10s lsblk -o NAME,PATH,TYPE,SIZE,RM,RO,TRAN,MODEL,FSTYPE,LABEL,MOUNTPOINTS -- "${device}" || true
fi
