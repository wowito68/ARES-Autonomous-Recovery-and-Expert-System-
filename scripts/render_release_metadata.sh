#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: render_release_metadata.sh BUILD_ROOT' >&2
    exit 2
fi

build_root=$1
template_root="${build_root}/config/includes.chroot/usr/share/ares/templates"

render() {
    source_file=$1
    target_file=$2
    sed \
        -e "s|@ARES_VERSION@|${ARES_VERSION}|g" \
        -e "s|@ARES_CHANNEL@|${ARES_CHANNEL}|g" \
        -e "s|@ARES_BUILD_ID@|${ARES_BUILD_ID}|g" \
        -e "s|@ARES_GIT_COMMIT@|${ARES_GIT_COMMIT}|g" \
        -e "s|@ARES_DEBIAN_POINT@|${ARES_DEBIAN_POINT}|g" \
        -e "s|@ARES_DEBIAN_CODENAME@|${ARES_DEBIAN_CODENAME}|g" \
        -e "s|@ARES_SOURCE_DATE_EPOCH@|${ARES_SOURCE_DATE_EPOCH}|g" \
        "${source_file}" > "${target_file}"
}

render "${template_root}/os-release.in" "${template_root}/os-release"
render "${template_root}/issue.in" "${build_root}/config/includes.chroot/etc/issue"
render "${template_root}/motd.in" "${build_root}/config/includes.chroot/etc/motd"
render "${template_root}/release.json.in" "${build_root}/config/includes.chroot/usr/share/ares/release.json"

rm -f "${template_root}"/*.in
touch -d "@${ARES_SOURCE_DATE_EPOCH}" \
    "${template_root}/os-release" \
    "${build_root}/config/includes.chroot/etc/issue" \
    "${build_root}/config/includes.chroot/etc/motd" \
    "${build_root}/config/includes.chroot/usr/share/ares/release.json"
