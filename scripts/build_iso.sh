#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./scripts/build_iso.sh [--config-only] [--keep-work]

Environment:
  ARES_CONTAINER_ENGINE  docker or podman (auto-detected by default)
  ARES_CLEAN_BUILD       1 to use an empty live-build cache
  ARES_REQUIRE_CLEAN     1 to reject a dirty Git worktree
  ARES_REUSE_BUILDER     1 to reuse an already verified pinned builder image
  ARES_SQUASHFS_PROCESSORS  compression workers (default: 2)
  ARES_SQUASHFS_MEMORY      compression cache limit (default: 512M)
EOF
}

config_only=0
keep_work=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --config-only) config_only=1 ;;
        --keep-work) keep_work=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

set -a
# shellcheck disable=SC1091
. "${repo_root}/live/release.env"
set +a

if [ "${ARES_REQUIRE_CLEAN:-0}" = "1" ] && [ -n "$(git -C "${repo_root}" status --porcelain)" ]; then
    printf '%s\n' 'Release build refused: the Git worktree is not clean.' >&2
    exit 1
fi

engine=${ARES_CONTAINER_ENGINE:-}
if [ -z "${engine}" ]; then
    if command -v docker >/dev/null 2>&1; then
        engine=docker
    elif command -v podman >/dev/null 2>&1; then
        engine=podman
    else
        printf '%s\n' 'docker or podman is required for the reproducible builder.' >&2
        exit 1
    fi
fi
case "${engine}" in
    docker|podman) ;;
    *) printf 'Unsupported container engine: %s\n' "${engine}" >&2; exit 1 ;;
esac

output_dir=${ARES_OUTPUT_DIR:-"${repo_root}/iso"}
persistent_cache="${repo_root}/live/.cache"
mkdir -p "${output_dir}" "${repo_root}/live/.build" "${persistent_cache}"

if ! command -v flock >/dev/null 2>&1; then
    printf '%s\n' 'flock is required to serialize ARES ISO builds.' >&2
    exit 1
fi
exec 9>"${repo_root}/live/.build/build.lock"
if ! flock -n 9; then
    printf '%s\n' 'Another ARES ISO build is already running for this repository.' >&2
    exit 1
fi

squashfs_processors=${ARES_SQUASHFS_PROCESSORS:-2}
squashfs_memory=${ARES_SQUASHFS_MEMORY:-512M}
case "${squashfs_processors}" in
    *[!0-9]*|'')
        printf '%s\n' 'ARES_SQUASHFS_PROCESSORS must be a positive integer.' >&2
        exit 1
        ;;
esac
if [ "${squashfs_processors}" -lt 1 ]; then
    printf '%s\n' 'ARES_SQUASHFS_PROCESSORS must be a positive integer.' >&2
    exit 1
fi
if ! printf '%s\n' "${squashfs_memory}" | grep -Eq '^[1-9][0-9]*[KMGkmg]?$'; then
    printf '%s\n' 'ARES_SQUASHFS_MEMORY must be a size such as 512M or 1G.' >&2
    exit 1
fi

output_dir=$(CDPATH= cd -- "${output_dir}" && pwd)
work_dir=$(mktemp -d "${repo_root}/live/.build/work.XXXXXX")
if [ "${ARES_CLEAN_BUILD:-0}" = "1" ]; then
    persist_cache=0
else
    persist_cache=1
fi

cleanup() {
    if [ "${keep_work}" != "1" ]; then
        find "${work_dir}" -mindepth 1 -delete 2>/dev/null || true
        rmdir "${work_dir}" 2>/dev/null || true
    else
        printf 'Build workspace retained at %s\n' "${work_dir}"
    fi
}
trap cleanup EXIT HUP INT TERM

git_commit=$(git -C "${repo_root}" rev-parse --verify HEAD 2>/dev/null || printf unknown)
if [ -n "$(git -C "${repo_root}" status --porcelain 2>/dev/null || true)" ]; then
    git_commit="${git_commit}-dirty"
fi
export ARES_GIT_COMMIT="${git_commit}"
export ARES_BUILD_ID="${ARES_VERSION}-${ARES_SOURCE_DATE_EPOCH}"

tag_suffix=$(printf '%s-%s' "${ARES_DEBIAN_POINT}" "${ARES_LIVE_BUILD_VERSION}" | tr ':+' '--')
builder_image="ares-live-builder:${tag_suffix}"

case "${ARES_REUSE_BUILDER:-0}" in
    0)
        printf 'Building pinned ARES builder (%s)...\n' "${builder_image}"
        if [ "${engine}" = "docker" ]; then
            "${engine}" build --load \
                --file "${repo_root}/live/Dockerfile.build" \
                --build-arg "DEBIAN_IMAGE=${ARES_BASE_IMAGE}" \
                --build-arg "DEBIAN_SNAPSHOT=${ARES_DEBIAN_SNAPSHOT}" \
                --build-arg "SECURITY_SNAPSHOT=${ARES_SECURITY_SNAPSHOT}" \
                --build-arg "LIVE_BUILD_VERSION=${ARES_LIVE_BUILD_VERSION}" \
                --tag "${builder_image}" \
                "${repo_root}"
        else
            "${engine}" build \
                --file "${repo_root}/live/Dockerfile.build" \
                --build-arg "DEBIAN_IMAGE=${ARES_BASE_IMAGE}" \
                --build-arg "DEBIAN_SNAPSHOT=${ARES_DEBIAN_SNAPSHOT}" \
                --build-arg "SECURITY_SNAPSHOT=${ARES_SECURITY_SNAPSHOT}" \
                --build-arg "LIVE_BUILD_VERSION=${ARES_LIVE_BUILD_VERSION}" \
                --tag "${builder_image}" \
                "${repo_root}"
        fi
        ;;
    1)
        printf 'Reusing and verifying pinned ARES builder (%s)...\n' "${builder_image}"
        if ! "${engine}" image inspect "${builder_image}" >/dev/null 2>&1; then
            printf 'Pinned builder image is not available: %s\n' "${builder_image}" >&2
            exit 1
        fi
        expected_label=${ARES_LIVE_BUILD_VERSION}
        actual_label=$("${engine}" image inspect "${builder_image}" \
            --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')
        expected_lb_version=${ARES_LIVE_BUILD_VERSION#*:}
        actual_lb_version=$("${engine}" run --rm --read-only --tmpfs /tmp:size=32m \
            "${builder_image}" lb --version)
        if [ "${actual_label}" != "${expected_label}" ] || \
           [ "${actual_lb_version}" != "${expected_lb_version}" ]; then
            printf 'Pinned builder verification failed (label=%s, live-build=%s).\n' \
                "${actual_label}" "${actual_lb_version}" >&2
            exit 1
        fi
        if ! "${engine}" run --rm --read-only --tmpfs /tmp:size=32m \
            "${builder_image}" sh -ec \
            'command -v lb mksquashfs veritysetup xorriso >/dev/null'; then
            printf '%s\n' 'Pinned builder verification failed: a required image tool is missing.' >&2
            exit 1
        fi
        ;;
    *)
        printf '%s\n' 'ARES_REUSE_BUILDER must be 0 or 1.' >&2
        exit 1
        ;;
esac

run_builder() {
    "${engine}" run --rm --privileged --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,size=256m \
        --tmpfs /run:rw,nosuid,nodev,size=64m \
        --mount "type=bind,src=${repo_root},dst=/workspace,readonly" \
        --mount "type=bind,src=${work_dir},dst=/build" \
        --mount "type=bind,src=${persistent_cache},dst=/cache-persistent" \
        --mount "type=bind,src=${output_dir},dst=/output" \
        --env "ARES_VERSION=${ARES_VERSION}" \
        --env "ARES_CHANNEL=${ARES_CHANNEL}" \
        --env "ARES_DEBIAN_CODENAME=${ARES_DEBIAN_CODENAME}" \
        --env "ARES_DEBIAN_POINT=${ARES_DEBIAN_POINT}" \
        --env "ARES_DEBIAN_SNAPSHOT=${ARES_DEBIAN_SNAPSHOT}" \
        --env "ARES_SECURITY_SNAPSHOT=${ARES_SECURITY_SNAPSHOT}" \
        --env "ARES_LIVE_BUILD_VERSION=${ARES_LIVE_BUILD_VERSION}" \
        --env "ARES_SOURCE_DATE_EPOCH=${ARES_SOURCE_DATE_EPOCH}" \
        --env "SOURCE_DATE_EPOCH=${ARES_SOURCE_DATE_EPOCH}" \
        --env "ARES_ARCH=${ARES_ARCH}" \
        --env "ARES_SQUASHFS_COMPRESSION=${ARES_SQUASHFS_COMPRESSION}" \
        --env "ARES_GIT_COMMIT=${ARES_GIT_COMMIT}" \
        --env "ARES_BUILD_ID=${ARES_BUILD_ID}" \
        --env "ARES_BASE_IMAGE=${ARES_BASE_IMAGE}" \
        --env "HOST_UID=$(id -u)" \
        --env "HOST_GID=$(id -g)" \
        --env "ARES_KEEP_WORK=${keep_work}" \
        --env "ARES_PERSIST_CACHE=${persist_cache}" \
        --env "MKSQUASHFS_OPTIONS=-processors ${squashfs_processors} -mem ${squashfs_memory}" \
        "${builder_image}" \
        /workspace/scripts/build_live_in_container.sh "$@"
}

printf 'Running live-build in an isolated workspace...\n'
if [ "${config_only}" = "1" ]; then
    run_builder --config-only
else
    run_builder
fi

if [ "${config_only}" = "1" ]; then
    printf '%s\n' 'live-build configuration is valid.'
else
    printf 'ARES OS image ready: %s\n' "${output_dir}/ARES.iso"
fi
