#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./scripts/vm_gui_probe.sh [options]

Options:
  --iso PATH              ISO to boot (default: iso/ARES.iso)
  --firmware bios|uefi-secure
                          Firmware path to exercise (default: uefi-secure)
  --boot cd|hybrid        Boot as optical media or ISO-hybrid USB disk (default: hybrid)
  --timeout SECONDS       Boot-ready timeout (default: 300)
  --settle SECONDS        Extra wait after boot-ready before screenshot (default: 15)
  --vnc-display :N        VNC display for live viewing (default: :11)
  --memory MB             Guest RAM in MiB (default: 4096)
  --smp N                 Guest CPUs (default: 4)
  --accel kvm|tcg|tcg,thread=multi
                          QEMU acceleration (default: kvm when available, else tcg,thread=multi)
  --evidence-dir DIR      Evidence directory (default: .vm/gui-TIMESTAMP)

Output:
  serial.log              Guest serial log
  screen.ppm              QEMU framebuffer screenshot
  screen.png              Screenshot converted when ImageMagick is available
  qemu.log                QEMU stderr/stdout
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

iso="${repo_root}/iso/ARES.iso"
firmware=uefi-secure
boot=hybrid
timeout_seconds=300
settle_seconds=15
vnc_display=:11
memory=4096
smp=4
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
        --timeout) timeout_seconds=$2; shift ;;
        --settle) settle_seconds=$2; shift ;;
        --vnc-display) vnc_display=$2; shift ;;
        --memory) memory=$2; shift ;;
        --smp) smp=$2; shift ;;
        --accel) accel=$2; shift ;;
        --evidence-dir) evidence_dir=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "${firmware}" in bios|uefi-secure) ;; *) printf 'Unknown firmware: %s\n' "${firmware}" >&2; exit 2 ;; esac
case "${boot}" in cd|hybrid) ;; *) printf 'Unknown boot mode: %s\n' "${boot}" >&2; exit 2 ;; esac
case "${accel}" in kvm|tcg|tcg,thread=multi) ;; *) printf 'Unsupported acceleration: %s\n' "${accel}" >&2; exit 2 ;; esac

iso=$(readlink -f -- "${iso}")
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null 2>&1 || { printf '%s\n' 'qemu-system-x86_64 is required.' >&2; exit 1; }

if [ -z "${evidence_dir}" ]; then
    evidence_dir="${repo_root}/.vm/gui-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "${evidence_dir}"

serial_log="${evidence_dir}/serial.log"
qemu_log="${evidence_dir}/qemu.log"
runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-vm-gui.XXXXXX")
qmp_socket="${runtime_dir}/qmp.sock"
screen_ppm="${evidence_dir}/screen.ppm"
screen_png="${evidence_dir}/screen.png"

qmp() {
    python3 - "$qmp_socket" "$@" <<'PY'
import json
import socket
import sys
import time

sock_path = sys.argv[1]
commands = sys.argv[2:]

def read_response(stream):
    deadline = time.time() + 10
    while time.time() < deadline:
        line = stream.readline()
        if not line:
            time.sleep(0.05)
            continue
        data = json.loads(line.decode("utf-8"))
        if "return" in data or "error" in data:
            return data
    raise SystemExit("QMP response timed out")

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.connect(sock_path)
    stream = sock.makefile("rwb", buffering=0)
    stream.readline()
    stream.write(json.dumps({"execute": "qmp_capabilities"}).encode("utf-8") + b"\r\n")
    read_response(stream)
    for raw in commands:
        stream.write(raw.encode("utf-8") + b"\r\n")
        result = read_response(stream)
        if "error" in result:
            raise SystemExit(json.dumps(result["error"]))
PY
}

cleanup() {
    if [ -n "${qemu_pid:-}" ] && kill -0 "${qemu_pid}" 2>/dev/null; then
        qmp '{"execute":"quit"}' 2>/dev/null || kill "${qemu_pid}" 2>/dev/null || true
        wait "${qemu_pid}" 2>/dev/null || true
    fi
    rm -rf "${runtime_dir}"
}
trap cleanup EXIT HUP INT TERM

set -- \
    -name ARES-OS-GUI-Probe \
    -m "${memory}" \
    -smp "${smp}" \
    -accel "${accel}" \
    -no-reboot \
    -nic none \
    -display "vnc=${vnc_display}" \
    -serial "file:${serial_log}" \
    -qmp "unix:${qmp_socket},server=on,wait=off"

if [ "${accel}" = kvm ]; then
    set -- "$@" -cpu host
fi

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

printf 'ARES GUI probe evidence: %s\n' "${evidence_dir}"
printf 'VNC display: %s\n' "${vnc_display}"
printf 'Serial log: %s\n' "${serial_log}"
printf 'QEMU runtime sockets: %s\n' "${runtime_dir}"
qemu-system-x86_64 "$@" >"${qemu_log}" 2>&1 &
qemu_pid=$!

deadline=$(( $(date +%s) + timeout_seconds ))
while kill -0 "${qemu_pid}" 2>/dev/null; do
    if grep -Fq 'ARES_BOOT_READY ' "${serial_log}" 2>/dev/null; then
        grep -F 'ARES_BOOT_READY ' "${serial_log}" | tail -n 1
        break
    fi
    if grep -Fq 'ARES_BOOT_FAILED ' "${serial_log}" 2>/dev/null; then
        grep -F 'ARES_BOOT_FAILED ' "${serial_log}" | tail -n 1 >&2
        exit 1
    fi
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        printf '%s\n' 'Timed out before ARES_BOOT_READY.' >&2
        exit 1
    fi
    sleep 1
done

if ! kill -0 "${qemu_pid}" 2>/dev/null; then
    printf '%s\n' 'QEMU exited before screenshot capture.' >&2
    exit 1
fi

sleep "${settle_seconds}"
qmp "{\"execute\":\"screendump\",\"arguments\":{\"filename\":\"${screen_ppm}\"}}"

if command -v magick >/dev/null 2>&1; then
    magick "${screen_ppm}" "${screen_png}" || true
elif command -v convert >/dev/null 2>&1; then
    convert "${screen_ppm}" "${screen_png}" || true
fi

qmp '{"execute":"quit"}' || true
wait "${qemu_pid}" 2>/dev/null || true
qemu_pid=

printf 'Screenshot: %s\n' "${screen_ppm}"
[ -s "${screen_png}" ] && printf 'Screenshot PNG: %s\n' "${screen_png}"
printf 'GUI probe completed. Evidence retained at %s\n' "${evidence_dir}"
