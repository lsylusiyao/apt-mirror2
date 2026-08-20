#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo 'Usage: stage-kylin-volume.sh MOUNTED_DISC_OR_VOLUME STAGING_DIR' >&2
    exit 64
fi

if command -v apt-mirror-offline >/dev/null 2>&1; then
    offline_cli=(apt-mirror-offline)
else
    offline_cli=(python3 -m apt_mirror.offline)
fi

set +e
"${offline_cli[@]}" stage "$1" "$2"
status=$?
set -e

# Incomplete multi-disc staging is expected, not a shell failure.
if [[ $status -eq 3 ]]; then
    echo 'More volumes are required; insert and stage the next disc.'
    exit 0
fi
exit "$status"
