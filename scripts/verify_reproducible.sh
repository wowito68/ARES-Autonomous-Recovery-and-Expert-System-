#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temp_root=$(mktemp -d "${TMPDIR:-/tmp}/ares-reproducible.XXXXXX")
passed=0

cleanup() {
    if [ "${passed}" -eq 1 ] || [ "${ARES_KEEP_REPRO_EVIDENCE:-0}" != 1 ]; then
        rm -rf -- "${temp_root}"
    else
        printf 'Reproducibility evidence retained at %s\n' "${temp_root}" >&2
    fi
}
trap cleanup EXIT HUP INT TERM

case "${ARES_REPRODUCIBLE_COLD_CACHE:-0}" in
    0) clean_build=0 ;;
    1) clean_build=1 ;;
    *)
        printf '%s\n' 'ARES_REPRODUCIBLE_COLD_CACHE must be 0 or 1.' >&2
        exit 2
        ;;
esac

mkdir -p "${temp_root}/first" "${temp_root}/second"
ARES_OUTPUT_DIR="${temp_root}/first" \
    ARES_CLEAN_BUILD="${clean_build}" \
    "${script_dir}/build_iso.sh"
ARES_OUTPUT_DIR="${temp_root}/second" \
    ARES_CLEAN_BUILD="${clean_build}" \
    "${script_dir}/build_iso.sh"

if cmp -s "${temp_root}/first/ARES.iso" "${temp_root}/second/ARES.iso"; then
    passed=1
    printf '%s\n' 'Reproducibility gate passed: both ISO images are byte-identical.'
    exit 0
fi

printf '%s\n' 'Reproducibility gate failed. SHA-256 values:' >&2
sha256sum "${temp_root}/first/ARES.iso" "${temp_root}/second/ARES.iso" >&2
printf '%s\n' 'Rerun with separate retained output directories and use diffoscope to locate nondeterministic metadata.' >&2
exit 1
