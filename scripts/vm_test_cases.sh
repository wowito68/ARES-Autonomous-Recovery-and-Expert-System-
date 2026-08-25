#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./scripts/vm_test_cases.sh [options]

Options:
  --iso PATH              ISO to test (default: iso/ARES.iso)
  --profile NAME          all|bios-cd|bios-hybrid|uefi-secure-cd|uefi-secure-hybrid
                          (default: all)
  --timeout SECONDS       Boot-ready timeout per profile (default: 300)
  --evidence-dir DIR      Keep VM evidence in DIR (default: .vm/cases-TIMESTAMP)

The script runs the serial boot smoke cases and keeps the logs so failures can
be inspected instead of disappearing with a temporary directory.
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

iso="${repo_root}/iso/ARES.iso"
profile=all
timeout_seconds=${ARES_QEMU_TIMEOUT:-300}
evidence_dir=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --iso) iso=$2; shift ;;
        --profile) profile=$2; shift ;;
        --timeout) timeout_seconds=$2; shift ;;
        --evidence-dir) evidence_dir=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "${profile}" in
    all|bios-cd|bios-hybrid|uefi-secure-cd|uefi-secure-hybrid) ;;
    *) printf 'Unknown profile: %s\n' "${profile}" >&2; exit 2 ;;
esac

if [ -z "${evidence_dir}" ]; then
    evidence_dir="${repo_root}/.vm/cases-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "${evidence_dir}"
iso=$(readlink -f -- "${iso}")

printf 'ARES VM case evidence: %s\n' "${evidence_dir}"
printf 'ISO: %s\n' "${iso}"
printf 'Profile: %s\n' "${profile}"

set +e
ARES_QEMU_PROFILE="${profile}" \
ARES_QEMU_TIMEOUT="${timeout_seconds}" \
ARES_QEMU_KEEP_EVIDENCE=1 \
ARES_QEMU_EVIDENCE_DIR="${evidence_dir}" \
    "${repo_root}/scripts/smoke_test_iso.sh" "${iso}" >"${evidence_dir}/smoke.log" 2>&1
status=$?
set -e

sed -n '1,240p' "${evidence_dir}/smoke.log"

if [ "${status}" -ne 0 ]; then
    printf 'VM cases failed. Evidence retained at %s\n' "${evidence_dir}" >&2
    exit "${status}"
fi

printf 'VM cases passed. Evidence retained at %s\n' "${evidence_dir}"
