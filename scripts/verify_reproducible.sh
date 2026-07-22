#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp_root=$(mktemp -d "${TMPDIR:-/tmp}/ares-reproducible.XXXXXX")
trap 'rm -rf -- "${temp_root}"' EXIT HUP INT TERM

mkdir -p "${temp_root}/first" "${temp_root}/second"
ARES_OUTPUT_DIR="${temp_root}/first" ARES_CLEAN_BUILD=1 "${script_dir}/build_iso.sh"
ARES_OUTPUT_DIR="${temp_root}/second" ARES_CLEAN_BUILD=1 "${script_dir}/build_iso.sh"

if cmp -s "${temp_root}/first/ARES.iso" "${temp_root}/second/ARES.iso"; then
    printf '%s\n' 'Reproducibility gate passed: both ISO images are byte-identical.'
    exit 0
fi

printf '%s\n' 'Reproducibility gate failed. SHA-256 values:' >&2
sha256sum "${temp_root}/first/ARES.iso" "${temp_root}/second/ARES.iso" >&2
printf '%s\n' 'Rerun with separate retained output directories and use diffoscope to locate nondeterministic metadata.' >&2
exit 1
