#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-verity-reproducible.XXXXXX")

cleanup() {
    find "${temp_dir}" -mindepth 1 -delete 2>/dev/null || true
    rmdir "${temp_dir}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

dd if=/dev/zero of="${temp_dir}/rootfs" bs=4096 count=8 status=none
"${script_dir}/veritysetup_reproducible.sh" \
    format "${temp_dir}/rootfs" "${temp_dir}/first.verity" \
    > "${temp_dir}/first.out"
"${script_dir}/veritysetup_reproducible.sh" \
    format "${temp_dir}/rootfs" "${temp_dir}/second.verity" \
    > "${temp_dir}/second.out"

cmp "${temp_dir}/first.verity" "${temp_dir}/second.verity"

expected_salt=$(sha256sum "${temp_dir}/rootfs" | awk '{print $1}')
actual_salt=$(veritysetup dump "${temp_dir}/first.verity" \
    | awk -F: '$1 == "Salt" {gsub(/[[:space:]]/, "", $2); print $2}')
[ "${actual_salt}" = "${expected_salt}" ] || {
    printf '%s\n' 'The dm-verity salt is not derived from the rootfs digest.' >&2
    exit 1
}

expected_uuid=$(printf '%s\n' "${expected_salt}" \
    | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/\1-\2-\3-\4-\5/')
actual_uuid=$(veritysetup dump "${temp_dir}/first.verity" \
    | awk -F: '$1 == "UUID" {gsub(/[[:space:]]/, "", $2); print $2}')
[ "${actual_uuid}" = "${expected_uuid}" ] || {
    printf '%s\n' 'The dm-verity UUID is not derived from the rootfs digest.' >&2
    exit 1
}

first_root_hash=$(awk -F: '$1 == "Root hash" {
    gsub(/[[:space:]]/, "", $2); print $2
}' "${temp_dir}/first.out")
second_root_hash=$(awk -F: '$1 == "Root hash" {
    gsub(/[[:space:]]/, "", $2); print $2
}' "${temp_dir}/second.out")
[ -n "${first_root_hash}" ] && [ "${first_root_hash}" = "${second_root_hash}" ] || {
    printf '%s\n' 'The dm-verity root hash is not reproducible.' >&2
    exit 1
}

printf '%s\n' 'ARES reproducible dm-verity fixture tests passed.'
