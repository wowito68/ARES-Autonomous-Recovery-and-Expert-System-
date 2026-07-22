#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    printf '%s\n' 'Usage: render_branding.sh BRANDING_DIR BUILD_ROOT' >&2
    exit 2
fi

branding_dir=$1
build_root=$2
rootfs="${build_root}/config/includes.chroot"
bootloaders="${build_root}/config/bootloaders"
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/tmp/ares-font-cache}
mkdir -p "${XDG_CACHE_HOME}"

command -v jq >/dev/null 2>&1 || { printf '%s\n' 'jq is required.' >&2; exit 1; }
command -v rsvg-convert >/dev/null 2>&1 || { printf '%s\n' 'rsvg-convert is required.' >&2; exit 1; }
jq -e '.id == "ares" and .name == "ARES OS" and .palette.primary == "#22D3EE"' "${branding_dir}/brand.json" >/dev/null

mkdir -p \
    "${bootloaders}/grub-pc" \
    "${bootloaders}/isolinux" \
    "${bootloaders}/syslinux_common" \
    "${rootfs}/usr/share/backgrounds/ares" \
    "${rootfs}/usr/share/icons/hicolor/scalable/apps" \
    "${rootfs}/usr/share/icons/hicolor/256x256/apps" \
    "${rootfs}/usr/share/pixmaps/ares" \
    "${rootfs}/usr/share/plymouth/themes/ares" \
    "${rootfs}/usr/share/ares/branding"

rsvg-convert -w 1024 -h 768 -o "${bootloaders}/splash.png" "${branding_dir}/boot-background.svg"
cp "${bootloaders}/splash.png" "${bootloaders}/grub-pc/splash.png"
rsvg-convert -w 640 -h 480 -o "${bootloaders}/isolinux/splash.png" "${branding_dir}/boot-background.svg"
cp "${bootloaders}/isolinux/splash.png" "${bootloaders}/syslinux_common/splash.png"

rsvg-convert -w 2560 -h 1440 -o "${rootfs}/usr/share/backgrounds/ares/ares-wallpaper.png" "${branding_dir}/wallpaper.svg"
cp "${branding_dir}/wallpaper.svg" "${rootfs}/usr/share/backgrounds/ares/ares-wallpaper.svg"
cp "${branding_dir}/ares-mark.svg" "${rootfs}/usr/share/icons/hicolor/scalable/apps/ares.svg"
rsvg-convert -w 256 -h 256 -o "${rootfs}/usr/share/icons/hicolor/256x256/apps/ares.png" "${branding_dir}/ares-mark.svg"
cp "${rootfs}/usr/share/icons/hicolor/256x256/apps/ares.png" "${rootfs}/usr/share/pixmaps/ares/avatar.png"
cp "${rootfs}/usr/share/icons/hicolor/256x256/apps/ares.png" "${rootfs}/usr/share/plymouth/themes/ares/ares-logo.png"
cp "${branding_dir}/brand.json" "${rootfs}/usr/share/ares/branding/brand.json"
cp "${branding_dir}/ares-mark.svg" "${rootfs}/usr/share/ares/branding/ares-mark.svg"
cp "${branding_dir}/LICENSE.md" "${rootfs}/usr/share/ares/branding/ARTWORK-LICENSE.md"

find "${bootloaders}" "${rootfs}/usr/share/backgrounds/ares" "${rootfs}/usr/share/icons/hicolor" "${rootfs}/usr/share/pixmaps/ares" "${rootfs}/usr/share/plymouth/themes/ares" "${rootfs}/usr/share/ares/branding" \
    -type f -exec touch -d "@${SOURCE_DATE_EPOCH}" {} +
