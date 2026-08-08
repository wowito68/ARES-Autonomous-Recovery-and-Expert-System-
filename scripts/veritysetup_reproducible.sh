#!/bin/sh
set -eu

real_veritysetup=${ARES_REAL_VERITYSETUP:-/usr/sbin/veritysetup}
data_device=
format_seen=0

for argument in "$@"; do
    if [ "${format_seen}" -eq 1 ]; then
        data_device=${argument}
        break
    fi
    if [ "${argument}" = format ]; then
        format_seen=1
    fi
done

if [ -z "${data_device}" ]; then
    exec "${real_veritysetup}" "$@"
fi
if [ ! -f "${data_device}" ]; then
    printf 'Cannot derive dm-verity salt from non-file input: %s\n' \
        "${data_device}" >&2
    exit 1
fi

salt=$(sha256sum "${data_device}" | awk '{print $1}')
uuid=$(printf '%s\n' "${salt}" \
    | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12}).*/\1-\2-\3-\4-\5/')
exec "${real_veritysetup}" --salt "${salt}" --uuid "${uuid}" "$@"
