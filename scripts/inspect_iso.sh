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
printf '%s' "${volume_info}" | grep -Eq "Volume Id[[:space:]]*:[[:space:]]*'ARES_OS_AMD64'" || { printf '%s\n' 'Unexpected ISO volume identifier.' >&2; exit 1; }

live_listing=$(xorriso -indev "${iso}" -ls /live 2>&1)
for payload in filesystem.squashfs filesystem.squashfs.verity filesystem.squashfs.roothash; do
    printf '%s\n' "${live_listing}" | grep -Fq "${payload}" || { printf 'Live payload is missing: %s\n' "${payload}" >&2; exit 1; }
done

temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ares-inspect.XXXXXX")
trap 'rm -rf -- "${temp_dir}"' EXIT HUP INT TERM
for payload in filesystem.squashfs filesystem.squashfs.verity filesystem.squashfs.roothash; do
    xorriso -osirrox on -indev "${iso}" \
        -extract "/live/${payload}" "${temp_dir}/${payload}" >/dev/null 2>&1
done
root_hash=$(tr -d '[:space:]' < "${temp_dir}/filesystem.squashfs.roothash")
printf '%s' "${root_hash}" | grep -Eq '^[0-9a-f]{64}$' || { printf '%s\n' 'Invalid dm-verity root hash.' >&2; exit 1; }
veritysetup verify \
    "${temp_dir}/filesystem.squashfs" \
    "${temp_dir}/filesystem.squashfs.verity" \
    "${root_hash}" >/dev/null

printf '%s\n' 'ISO boot metadata and dm-verity inspection passed.'
