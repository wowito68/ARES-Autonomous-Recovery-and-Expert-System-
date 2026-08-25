#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./scripts/run_vm_iso.sh [options]

Options:
  --iso PATH              ISO to boot (default: iso/ARES.iso)
  --firmware bios|uefi-secure
                          Firmware path to exercise (default: uefi-secure)
  --boot cd|hybrid        Boot as optical media or ISO-hybrid USB disk (default: hybrid)
  --display gtk|sdl|vnc|none
                          QEMU display backend (default: gtk)
  --vnc-display :N        VNC display when --display vnc (default: :10)
  --memory MB             Guest RAM in MiB (default: 4096)
  --smp N                 Guest CPUs (default: 4)
  --net none|user         Guest network mode (default: none)
  --accel kvm|tcg|tcg,thread=multi
                          QEMU acceleration (default: tcg,thread=multi)
  --evidence-dir DIR      Directory for logs/sockets (default: .vm/manual-TIMESTAMP)

Examples:
  ./scripts/run_vm_iso.sh --display gtk
  ./scripts/run_vm_iso.sh --display vnc --vnc-display :10
  ARES_QEMU_ACCEL=kvm ./scripts/run_vm_iso.sh --display gtk
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

iso="${repo_root}/iso/ARES.iso"
firmware=uefi-secure
boot=hybrid
display=gtk
vnc_display=:10
memory=4096
smp=4
net=none
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    accel=${ARES_QEMU_ACCEL:-kvm}
else
    accel=${ARES_QEMU_ACCEL:-tcg,thread=multi}
fi
evidence_dir=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --iso) iso=$2; shift ;;
        --firmware) firmware=$2; shift ;;
        --boot) boot=$2; shift ;;
        --display) display=$2; shift ;;
        --vnc-display) vnc_display=$2; shift ;;
        --memory) memory=$2; shift ;;
        --smp) smp=$2; shift ;;
        --net) net=$2; shift ;;
        --accel) accel=$2; shift ;;
        --evidence-dir) evidence_dir=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "${firmware}" in bios|uefi-secure) ;; *) printf 'Unknown firmware: %s\n' "${firmware}" >&2; exit 2 ;; esac
case "${boot}" in cd|hybrid) ;; *) printf 'Unknown boot mode: %s\n' "${boot}" >&2; exit 2 ;; esac
case "${display}" in gtk|sdl|vnc|none) ;; *) printf 'Unknown display: %s\n' "${display}" >&2; exit 2 ;; esac
case "${net}" in none|user) ;; *) printf 'Unknown network mode: %s\n' "${net}" >&2; exit 2 ;; esac
case "${accel}" in kvm|tcg|tcg,thread=multi) ;; *) printf 'Unsupported acceleration: %s\n' "${accel}" >&2; exit 2 ;; esac

iso=$(readlink -f -- "${iso}")
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null 2>&1 || { printf '%s\n' 'qemu-system-x86_64 is required.' >&2; exit 1; }

if [ -z "${evidence_dir}" ]; then
    evidence_dir="${repo_root}/.vm/manual-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "${evidence_dir}"
serial_log="${evidence_dir}/serial.log"
runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-vm.XXXXXX")
qmp_socket="${runtime_dir}/qmp.sock"
monitor_socket="${runtime_dir}/monitor.sock"

cleanup() {
    rm -rf "${runtime_dir}"
}
trap cleanup EXIT HUP INT TERM

set -- \
    -name ARES-OS \
    -m "${memory}" \
    -smp "${smp}" \
    -accel "${accel}" \
    -no-reboot \
    -serial "file:${serial_log}" \
    -qmp "unix:${qmp_socket},server=on,wait=off" \
    -monitor "unix:${monitor_socket},server=on,wait=off"

if [ "${accel}" = kvm ]; then
    set -- "$@" -cpu host
fi

if [ "${net}" = none ]; then
    set -- "$@" -nic none
else
    set -- "$@" -nic user,model=virtio-net-pci
fi

case "${display}" in
    gtk) set -- "$@" -display gtk,show-cursor=on ;;
    sdl) set -- "$@" -display sdl ;;
    vnc) set -- "$@" -display "vnc=${vnc_display}" ;;
    none) set -- "$@" -display none ;;
esac

if [ "${firmware}" = uefi-secure ]; then
    ovmf_code=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_CODE_4M.secboot.fd' -print 2>/dev/null | sort | head -n 1)
    ovmf_vars=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_VARS_4M.ms.fd' -print 2>/dev/null | sort | head -n 1)
    [ -n "${ovmf_code}" ] && [ -n "${ovmf_vars}" ] || {
        printf '%s\n' 'Secure Boot OVMF firmware with Microsoft-enrolled VARS is required.' >&2
        exit 1
    }
    cp "${ovmf_vars}" "${evidence_dir}/OVMF_VARS.fd"
    set -- "$@" \
        -machine q35,smm=on \
        -global driver=cfi.pflash01,property=secure,value=on \
        -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
        -drive "if=pflash,format=raw,file=${evidence_dir}/OVMF_VARS.fd"
else
    set -- "$@" -machine pc
fi

if [ "${boot}" = cd ]; then
    set -- "$@" -boot d -cdrom "${iso}"
else
    if [ "${firmware}" = bios ]; then
        set -- "$@" -boot c -drive "file=${iso},format=raw,if=ide,media=disk,snapshot=on"
    else
        set -- "$@" -boot c -drive "file=${iso},format=raw,if=virtio,media=disk,snapshot=on"
    fi
fi

printf 'ARES VM evidence: %s\n' "${evidence_dir}"
printf 'Serial log: %s\n' "${serial_log}"
printf 'QEMU runtime sockets: %s\n' "${runtime_dir}"
if [ "${display}" = vnc ]; then
    printf 'VNC display: %s\n' "${vnc_display}"
fi
printf '%s\n' 'Starting QEMU. Close the VM window or press Ctrl+C to stop.'
qemu-system-x86_64 "$@"
