#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: smoke_test_iso.sh PATH_TO_ISO' >&2
    exit 2
fi

iso=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null 2>&1 || { printf '%s\n' 'qemu-system-x86_64 is required.' >&2; exit 1; }

timeout_seconds=${ARES_QEMU_TIMEOUT:-90}
temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-qemu.XXXXXX")
failed=0
cleanup() {
    if [ "${failed}" -eq 0 ]; then
        rm -rf -- "${temp_dir}"
    else
        printf 'QEMU evidence retained at %s\n' "${temp_dir}" >&2
    fi
}
trap cleanup EXIT HUP INT TERM

fail_boot() {
    failed=1
    name=$1
    message=$2
    printf '%s: %s\n' "${name}" "${message}" >&2
    sed -n '1,240p' "${temp_dir}/${name}.log" >&2
    exit 1
}

run_boot() {
    name=$1
    expected_trust=$2
    shift 2
    log="${temp_dir}/${name}.log"

    qemu-system-x86_64 "$@" >"${log}" 2>&1 &
    qemu_pid=$!
    deadline=$(( $(date +%s) + timeout_seconds ))

    while kill -0 "${qemu_pid}" 2>/dev/null; do
        if grep -Fq 'ARES_BOOT_READY ' "${log}"; then
            if ! grep -Fq "trust=${expected_trust}" "${log}"; then
                kill "${qemu_pid}" 2>/dev/null || true
                wait "${qemu_pid}" 2>/dev/null || true
                fail_boot "${name}" "boot marker reported an unexpected integrity state"
            fi
            kill "${qemu_pid}" 2>/dev/null || true
            wait "${qemu_pid}" 2>/dev/null || true
            printf '%s guest boot passed (%s).\n' "${name}" "${expected_trust}"
            return 0
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            kill "${qemu_pid}" 2>/dev/null || true
            wait "${qemu_pid}" 2>/dev/null || true
            fail_boot "${name}" "timed out before the guest boot-ready marker"
        fi
        sleep 1
    done

    wait "${qemu_pid}" 2>/dev/null || qemu_status=$?
    fail_boot "${name}" "QEMU exited before the guest boot-ready marker (status ${qemu_status:-0})"
}

common_args='-m 2048 -nic none -display none -serial stdio -no-reboot'
# shellcheck disable=SC2086
run_boot bios-cd UNAVAILABLE \
    -machine accel=tcg ${common_args} \
    -boot d \
    -cdrom "${iso}"

# Exercise the ISO-hybrid system area as a disk, matching a raw USB write.
# shellcheck disable=SC2086
run_boot bios-hybrid UNAVAILABLE \
    -machine accel=tcg ${common_args} \
    -boot c \
    -drive "file=${iso},format=raw,if=ide,media=disk,readonly=on"

ovmf_code=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_CODE_4M.secboot.fd' -print 2>/dev/null | sort | head -n 1)
ovmf_vars=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_VARS_4M.ms.fd' -print 2>/dev/null | sort | head -n 1)
if [ -z "${ovmf_code}" ] || [ -z "${ovmf_vars}" ]; then
    failed=1
    printf '%s\n' 'Secure Boot OVMF firmware with Microsoft-enrolled VARS is required.' >&2
    exit 1
fi

cp "${ovmf_vars}" "${temp_dir}/OVMF_VARS_CD.fd"
# shellcheck disable=SC2086
run_boot uefi-secure-cd ENFORCED_PARTIAL \
    -machine q35,smm=on,accel=tcg ${common_args} \
    -global driver=cfi.pflash01,property=secure,value=on \
    -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
    -drive "if=pflash,format=raw,file=${temp_dir}/OVMF_VARS_CD.fd" \
    -boot d \
    -cdrom "${iso}"

cp "${ovmf_vars}" "${temp_dir}/OVMF_VARS_HYBRID.fd"
# shellcheck disable=SC2086
run_boot uefi-secure-hybrid ENFORCED_PARTIAL \
    -machine q35,smm=on,accel=tcg ${common_args} \
    -global driver=cfi.pflash01,property=secure,value=on \
    -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
    -drive "if=pflash,format=raw,file=${temp_dir}/OVMF_VARS_HYBRID.fd" \
    -boot c \
    -drive "file=${iso},format=raw,if=virtio,readonly=on"

printf '%s\n' 'BIOS, UEFI Secure Boot, optical, and ISO-hybrid guest boots passed.'
