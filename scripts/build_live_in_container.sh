#!/bin/sh
set -eu
umask 022
publish_dir=

cleanup_build_workspace() {
    if [ -n "${publish_dir}" ] && [ -d "${publish_dir}" ]; then
        case "${publish_dir}" in
            /output/.ares-publish.*)
                find "${publish_dir}" -mindepth 1 -delete 2>/dev/null || true
                rmdir "${publish_dir}" 2>/dev/null || true
                ;;
        esac
    fi
    if [ "${ARES_PERSIST_CACHE:-0}" = "1" ] && [ -d /build/cache ]; then
        if ! cp -a /build/cache/. /cache-persistent/; then
            printf '%s\n' 'Warning: failed to synchronize the live-build cache.' >&2
        fi
    fi
    if [ "${ARES_KEEP_WORK:-0}" = "1" ]; then
        printf '%s\n' 'Build workspace ownership preserved for forensic inspection.'
    else
        find /build -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    fi
}
trap cleanup_build_workspace EXIT HUP INT TERM

config_only=0
if [ "${1:-}" = "--config-only" ]; then
    config_only=1
fi

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'live-build must run as root inside the isolated builder.' >&2
    exit 1
fi

for required in /workspace/live/auto /workspace/live/config /workspace/live/release.env; do
    if [ ! -e "${required}" ]; then
        printf 'Missing build input: %s\n' "${required}" >&2
        exit 1
    fi
done

mkdir -p /build/cache
if [ "${ARES_PERSIST_CACHE:-0}" = "1" ]; then
    cp -a /cache-persistent/. /build/cache/
fi

cp -a /workspace/live/auto /build/
cp -a /workspace/live/config /build/
mkdir -p /build/config/packages.chroot /build/config/includes.chroot/usr/share/doc/ares-os
mkdir -p /build/config/includes.chroot/opt/ares/backend/src
cp -a /workspace/backend/src/ares /build/config/includes.chroot/opt/ares/backend/src/
find /build/config/includes.chroot/opt/ares/backend/src \
    -type d -name __pycache__ -prune -exec rm -rf -- {} +

if find /workspace/live/packages/debs -maxdepth 1 -type f -name '*.deb' -print -quit 2>/dev/null | grep -q .; then
    if [ "${ARES_CHANNEL}" != "development" ]; then
        printf '%s\n' 'Release channels must consume a signed APT repository, not packages.chroot.' >&2
        exit 1
    fi
    cp /workspace/live/packages/debs/*.deb /build/config/packages.chroot/
fi

if [ -d /workspace/live/models/runtime ] || [ -d /workspace/live/models/store ]; then
    if [ ! -s /workspace/live/models/BUNDLE.json ]; then
        printf '%s\n' 'An offline AI bundle requires live/models/BUNDLE.json.' >&2
        exit 1
    fi
    if [ ! -s /workspace/live/models/SHA256SUMS ]; then
        printf '%s\n' 'An offline AI bundle requires live/models/SHA256SUMS.' >&2
        exit 1
    fi
    if ! jq -e '
        .schema_version == 1 and
        .architecture == "amd64" and
        .runtime.name == "ollama" and
        (.runtime.version | type == "string" and length > 0) and
        (.runtime.source | type == "string" and startswith("https://")) and
        (.runtime.license | type == "string" and length > 0) and
        .model.name == "qwen2.5:1.5b-instruct-q4_K_M" and
        (.model.source | type == "string" and startswith("https://")) and
        (.model.license | type == "string" and length > 0)
    ' /workspace/live/models/BUNDLE.json >/dev/null; then
        printf '%s\n' 'The offline AI BUNDLE.json metadata is incomplete or invalid.' >&2
        exit 1
    fi
    if find /workspace/live/models/runtime /workspace/live/models/store \
        -type l -print -quit 2>/dev/null | grep -q .; then
        printf '%s\n' 'Symlinks are forbidden inside the offline AI bundle.' >&2
        exit 1
    fi
    actual_ai_files=/tmp/ares-ai-actual-files
    declared_ai_files=/tmp/ares-ai-declared-files
    (
        cd /workspace/live/models
        printf '%s\n' BUNDLE.json
        find runtime store -type f -print 2>/dev/null | LC_ALL=C sort
    ) > "${actual_ai_files}"
    awk 'NF == 2 {name=$2; sub(/^\*/, "", name); print name}' \
        /workspace/live/models/SHA256SUMS | LC_ALL=C sort -u > "${declared_ai_files}"
    if ! cmp -s "${actual_ai_files}" "${declared_ai_files}"; then
        printf '%s\n' 'SHA256SUMS must declare every and only offline AI bundle file.' >&2
        exit 1
    fi
    (
        cd /workspace/live/models
        sha256sum --check --strict SHA256SUMS
    )
    if [ ! -x /workspace/live/models/runtime/bin/ollama ]; then
        printf '%s\n' 'The AI bundle lacks executable runtime/bin/ollama.' >&2
        exit 1
    fi
    if ! file /workspace/live/models/runtime/bin/ollama \
        | grep -Eq 'ELF 64-bit LSB.*x86-64'; then
        printf '%s\n' 'The AI runtime must be an amd64 ELF executable.' >&2
        exit 1
    fi
    if [ ! -s \
        /workspace/live/models/store/manifests/registry.ollama.ai/library/qwen2.5/1.5b-instruct-q4_K_M
    ]; then
        printf '%s\n' 'The offline AI bundle lacks the configured model manifest.' >&2
        exit 1
    fi
    mkdir -p \
        /build/config/includes.chroot/opt/ares/llm/runtime \
        /build/config/includes.chroot/usr/share/ares/ai \
        /build/config/includes.chroot/var/lib/ares/models
    cp -a /workspace/live/models/runtime/. \
        /build/config/includes.chroot/opt/ares/llm/runtime/
    cp /workspace/live/models/BUNDLE.json \
        /build/config/includes.chroot/usr/share/ares/ai/BUNDLE.json
    if [ -d /workspace/live/models/store ]; then
        cp -a /workspace/live/models/store/. \
            /build/config/includes.chroot/var/lib/ares/models/
    fi
    chown -R 0:0 /build/config/includes.chroot/opt/ares/llm/runtime
    chown -R 977:977 /build/config/includes.chroot/var/lib/ares/models
    find \
        /build/config/includes.chroot/opt/ares/llm/runtime \
        /build/config/includes.chroot/var/lib/ares/models \
        -type d -exec chmod go-w -- {} +
    find \
        /build/config/includes.chroot/opt/ares/llm/runtime \
        /build/config/includes.chroot/var/lib/ares/models \
        -type f -exec chmod go-w -- {} +
fi

cp /workspace/docs/*.md /build/config/includes.chroot/usr/share/doc/ares-os/
if [ -d /workspace/docs/adr ]; then
    mkdir -p /build/config/includes.chroot/usr/share/doc/ares-os/adr
    cp /workspace/docs/adr/*.md /build/config/includes.chroot/usr/share/doc/ares-os/adr/
fi
if [ -d /workspace/docs/diagrams ]; then
    mkdir -p /build/config/includes.chroot/usr/share/doc/ares-os/diagrams
    cp /workspace/docs/diagrams/*.mmd /build/config/includes.chroot/usr/share/doc/ares-os/diagrams/ 2>/dev/null || true
fi

/workspace/scripts/render_branding.sh /workspace/live/branding /build
/workspace/scripts/render_release_metadata.sh /build

cd /build
config_log=/tmp/ares-lb-config.log
if ! lb config >"${config_log}" 2>&1; then
    cat "${config_log}" >&2
    exit 1
fi
cat "${config_log}"
if grep -q '^E:' "${config_log}"; then
    printf '%s\n' 'live-build reported a configuration error.' >&2
    exit 1
fi

if [ "${config_only}" = "1" ]; then
    validation_log=/tmp/ares-lb-validate.log
    if ! lb config --validate >"${validation_log}" 2>&1; then
        cat "${validation_log}" >&2
        exit 1
    fi
    cat "${validation_log}"
    if grep -q '^E:' "${validation_log}"; then
        printf '%s\n' 'live-build validation reported an error.' >&2
        exit 1
    fi
    exit 0
fi

lb build

iso_path=$(find /build -maxdepth 1 -type f -name '*.iso' -print | sort | head -n 1)
if [ -z "${iso_path}" ] || [ ! -s "${iso_path}" ]; then
    printf '%s\n' 'live-build completed without producing an ISO.' >&2
    exit 1
fi

package_manifest=$(find /build -maxdepth 2 -type f \( -name 'chroot.packages.live' -o -name 'filesystem.packages' \) -print | sort | head -n 1)
if [ -z "${package_manifest}" ] || [ ! -s "${package_manifest}" ]; then
    printf '%s\n' 'live-build completed without a non-empty package manifest.' >&2
    exit 1
fi

publish_dir=$(mktemp -d /output/.ares-publish.XXXXXX)
install -m 0644 "${iso_path}" "${publish_dir}/ARES.iso"
LC_ALL=C sort -u "${package_manifest}" > "${publish_dir}/ARES.packages.txt"

iso_sha256=$(sha256sum "${publish_dir}/ARES.iso" | awk '{print $1}')
package_sha256=$(sha256sum "${publish_dir}/ARES.packages.txt" | awk '{print $1}')
jq -n \
    --arg version "${ARES_VERSION}" \
    --arg channel "${ARES_CHANNEL}" \
    --arg build_id "${ARES_BUILD_ID}" \
    --arg architecture "${ARES_ARCH}" \
    --arg debian_codename "${ARES_DEBIAN_CODENAME}" \
    --arg debian_point "${ARES_DEBIAN_POINT}" \
    --arg debian_snapshot "${ARES_DEBIAN_SNAPSHOT}" \
    --arg security_snapshot "${ARES_SECURITY_SNAPSHOT}" \
    --arg live_build "$(lb --version)" \
    --arg base_image "${ARES_BASE_IMAGE}" \
    --arg git_commit "${ARES_GIT_COMMIT}" \
    --arg source_date_epoch "${SOURCE_DATE_EPOCH}" \
    --arg iso_sha256 "${iso_sha256}" \
    --arg packages_sha256 "${package_sha256}" \
    '{
      schema_version: 1,
      product: "ARES OS",
      version: $version,
      channel: $channel,
      build_id: $build_id,
      architecture: $architecture,
      base: {debian_codename: $debian_codename, debian_point: $debian_point},
      snapshots: {debian: $debian_snapshot, security: $security_snapshot},
      builder: {live_build: $live_build, base_image: $base_image},
      source: {git_commit: $git_commit, source_date_epoch: ($source_date_epoch | tonumber)},
      artifacts: {
        iso: {name: "ARES.iso", sha256: $iso_sha256},
        packages: {name: "ARES.packages.txt", sha256: $packages_sha256}
      },
      integrity_claim: "development-partial"
    }' > "${publish_dir}/ARES.build.json"

(
    cd "${publish_dir}"
    sha256sum ARES.iso > ARES.iso.sha256
)

touch -d "@${SOURCE_DATE_EPOCH}" \
    "${publish_dir}/ARES.iso" \
    "${publish_dir}/ARES.iso.sha256" \
    "${publish_dir}/ARES.packages.txt" \
    "${publish_dir}/ARES.build.json"
chown "${HOST_UID}:${HOST_GID}" \
    "${publish_dir}/ARES.iso" \
    "${publish_dir}/ARES.iso.sha256" \
    "${publish_dir}/ARES.packages.txt" \
    "${publish_dir}/ARES.build.json"

# Publish metadata first and the ISO last. A visible ARES.iso therefore means
# every sidecar for the same completed build is already in place.
mv -f "${publish_dir}/ARES.packages.txt" /output/ARES.packages.txt
mv -f "${publish_dir}/ARES.build.json" /output/ARES.build.json
mv -f "${publish_dir}/ARES.iso.sha256" /output/ARES.iso.sha256
mv -f "${publish_dir}/ARES.iso" /output/ARES.iso
rmdir "${publish_dir}"
publish_dir=
