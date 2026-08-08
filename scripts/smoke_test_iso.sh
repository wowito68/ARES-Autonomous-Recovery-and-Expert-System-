#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: smoke_test_iso.sh PATH_TO_ISO' >&2
    exit 2
fi

iso=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
[ -s "${iso}" ] || { printf 'ISO not found or empty: %s\n' "${iso}" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null 2>&1 || { printf '%s\n' 'qemu-system-x86_64 is required.' >&2; exit 1; }

timeout_seconds=${ARES_QEMU_TIMEOUT:-300}
profile=${ARES_QEMU_PROFILE:-all}
accel=${ARES_QEMU_ACCEL:-tcg,thread=multi}
case "${profile}" in
    all|bios-cd|bios-hybrid|uefi-secure-cd|uefi-secure-hybrid) ;;
    *)
        printf 'Unknown ARES_QEMU_PROFILE: %s\n' "${profile}" >&2
        exit 2
        ;;
esac
case "${accel}" in
    kvm|tcg|tcg,thread=multi) ;;
    *)
        printf 'Unsupported ARES_QEMU_ACCEL: %s\n' "${accel}" >&2
        exit 2
        ;;
esac
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
    started_at=$(date +%s)

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
            if ! grep -Fq 'api=READY ui=READY hardware=READY' "${log}"; then
                kill "${qemu_pid}" 2>/dev/null || true
                wait "${qemu_pid}" 2>/dev/null || true
                fail_boot "${name}" "backend, static interface, or hardware inventory did not become ready"
            fi
            kill "${qemu_pid}" 2>/dev/null || true
            wait "${qemu_pid}" 2>/dev/null || true
            elapsed=$(( $(date +%s) - started_at ))
            printf '%s guest boot passed (%s, %ss).\n' \
                "${name}" "${expected_trust}" "${elapsed}"
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

selected() {
    [ "${profile}" = all ] || [ "${profile}" = "$1" ]
}

common_args="-m 2048 -smp 4 -accel ${accel} -nic none -display none -serial stdio -no-reboot"
if selected bios-cd; then
    # shellcheck disable=SC2086
    run_boot bios-cd UNAVAILABLE \
        -machine pc ${common_args} \
        -boot d \
        -cdrom "${iso}"
fi

# Exercise the ISO-hybrid system area as a disk, matching a raw USB write.
if selected bios-hybrid; then
    # shellcheck disable=SC2086
    run_boot bios-hybrid UNAVAILABLE \
        -machine pc ${common_args} \
        -boot c \
        -drive "file=${iso},format=raw,if=ide,media=disk,snapshot=on"
fi

case "${profile}" in
    all|uefi-secure-cd|uefi-secure-hybrid)
        ovmf_code=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_CODE_4M.secboot.fd' -print 2>/dev/null | sort | head -n 1)
        ovmf_vars=$(find /usr/share/OVMF /usr/share/ovmf -type f -name 'OVMF_VARS_4M.ms.fd' -print 2>/dev/null | sort | head -n 1)
        if [ -z "${ovmf_code}" ] || [ -z "${ovmf_vars}" ]; then
            failed=1
            printf '%s\n' 'Secure Boot OVMF firmware with Microsoft-enrolled VARS is required.' >&2
            exit 1
        fi
        ;;
esac

if selected uefi-secure-cd; then
    cp "${ovmf_vars}" "${temp_dir}/OVMF_VARS_CD.fd"
    # shellcheck disable=SC2086
    run_boot uefi-secure-cd ENFORCED_PARTIAL \
        -machine q35,smm=on ${common_args} \
        -global driver=cfi.pflash01,property=secure,value=on \
        -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
        -drive "if=pflash,format=raw,file=${temp_dir}/OVMF_VARS_CD.fd" \
        -boot d \
        -cdrom "${iso}"
fi

if selected uefi-secure-hybrid; then
    cp "${ovmf_vars}" "${temp_dir}/OVMF_VARS_HYBRID.fd"
    # shellcheck disable=SC2086
    run_boot uefi-secure-hybrid ENFORCED_PARTIAL \
        -machine q35,smm=on ${common_args} \
        -global driver=cfi.pflash01,property=secure,value=on \
        -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
        -drive "if=pflash,format=raw,file=${temp_dir}/OVMF_VARS_HYBRID.fd" \
        -boot c \
        -drive "file=${iso},format=raw,if=virtio,snapshot=on"
fi

if [ "${profile}" = all ]; then
    printf '%s\n' 'BIOS, UEFI Secure Boot, optical, and ISO-hybrid guest boots passed.'
else
    printf 'Selected QEMU boot profile passed: %s.\n' "${profile}"
fi
