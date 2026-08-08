#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)
collector=${repo_root}/live/config/includes.chroot/usr/lib/ares/ares-boot-integrity
test_root=$(mktemp -d "${TMPDIR:-/tmp}/ares-integrity-test.XXXXXX")

cleanup() {
    case "${test_root}" in
        "${TMPDIR:-/tmp}"/ares-integrity-test.*)
            rm -rf -- "${test_root}"
            ;;
    esac
}
trap cleanup EXIT HUP INT TERM

run_collector() {
    fixture=$1
    env \
        ARES_RUNTIME_ROOT="${fixture}/runtime" \
        ARES_EFI_ROOT="${fixture}/efi-unavailable" \
        ARES_PROC_MOUNTS="${fixture}/proc.mounts" \
        ARES_SYS_BLOCK_ROOT="${fixture}/sys/block" \
        "${collector}"
}

active=${test_root}/active
mkdir -p "${active}/sys/block/dm-0/dm"
printf '%s\n' \
    '/dev/mapper/ares-root-verity /run/live/rootfs/filesystem.squashfs squashfs ro,noatime,errors=continue 0 0' \
    > "${active}/proc.mounts"
printf '%s\n' 'ares-root-verity' > "${active}/sys/block/dm-0/dm/name"
printf '%s\n' 'CRYPT-VERITY-test-ares-root-verity' > "${active}/sys/block/dm-0/dm/uuid"
run_collector "${active}"
grep -Fq '"dm_verity":"active"' "${active}/runtime/integrity.json"
grep -Fq '"trust":"UNAVAILABLE"' "${active}/runtime/integrity.json"

invalid=${test_root}/invalid
mkdir -p "${invalid}/sys/block/dm-0/dm"
printf '%s\n' \
    '/dev/mapper/ares-root-verity /run/live/rootfs/filesystem.squashfs squashfs ro,noatime,errors=continue 0 0' \
    > "${invalid}/proc.mounts"
printf '%s\n' 'ares-root-verity' > "${invalid}/sys/block/dm-0/dm/name"
printf '%s\n' 'UNTRUSTED-test-ares-root-verity' > "${invalid}/sys/block/dm-0/dm/uuid"
if run_collector "${invalid}"; then
    printf '%s\n' 'Invalid dm identity was accepted.' >&2
    exit 1
fi
grep -Fq '"dm_verity":"unknown"' "${invalid}/runtime/integrity.json"
grep -Fq '"trust":"FAILED"' "${invalid}/runtime/integrity.json"

inactive=${test_root}/inactive
mkdir -p "${inactive}/sys/block"
printf '%s\n' \
    '/dev/loop0 /run/live/rootfs/filesystem.squashfs squashfs ro,noatime,errors=continue 0 0' \
    > "${inactive}/proc.mounts"
if run_collector "${inactive}"; then
    printf '%s\n' 'A non-verity Live root was accepted.' >&2
    exit 1
fi
grep -Fq '"dm_verity":"inactive"' "${inactive}/runtime/integrity.json"
grep -Fq '"trust":"FAILED"' "${inactive}/runtime/integrity.json"

printf '%s\n' 'ARES boot-integrity fixture tests passed.'
