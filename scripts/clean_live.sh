#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

case "${repo_root}" in
    /|'') printf '%s\n' 'Refusing to clean an unsafe repository path.' >&2; exit 1 ;;
esac

set -a
# shellcheck disable=SC1091
. "${repo_root}/live/release.env"
set +a

clean_directory() {
    target=$1
    [ -d "${target}" ] || return 0
    [ ! -L "${target}" ] || { printf 'Refusing to clean symlink: %s\n' "${target}" >&2; exit 1; }

    if find "${target}" -mindepth 1 -delete 2>/dev/null; then
        rmdir "${target}"
        return 0
    fi

    if command -v docker >/dev/null 2>&1; then
        docker run --rm --mount "type=bind,src=${target},dst=/ares-clean" "${ARES_BASE_IMAGE}" \
            find /ares-clean -mindepth 1 -delete
    elif command -v podman >/dev/null 2>&1; then
        podman run --rm --mount "type=bind,src=${target},dst=/ares-clean" "${ARES_BASE_IMAGE}" \
            find /ares-clean -mindepth 1 -delete
    else
        printf 'Root-owned build residue remains at %s; docker or podman is required to remove it safely.\n' "${target}" >&2
        exit 1
    fi
    rmdir "${target}"
}

clean_directory "${repo_root}/live/.build"
clean_directory "${repo_root}/live/.cache"
find "${repo_root}/iso" -maxdepth 1 -type f \
    \( -name 'ARES.iso' -o -name 'ARES.iso.sha256' -o -name 'ARES.packages.txt' -o -name 'ARES.build.json' -o -name 'ARES.spdx.json' \) \
    -delete 2>/dev/null || true

printf '%s\n' 'Removed generated ARES OS workspaces, cache and release artifacts.'
