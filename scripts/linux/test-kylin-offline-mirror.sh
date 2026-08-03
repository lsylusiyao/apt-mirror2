#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo 'Usage: test-kylin-offline-mirror.sh MIRROR_ROOT FEEDBACK_DIR' >&2
    exit 64
fi

if command -v apt-mirror-offline >/dev/null 2>&1; then
    offline_cli=(apt-mirror-offline)
elif python3 -c 'import apt_mirror.offline' >/dev/null 2>&1; then
    offline_cli=(python3 -m apt_mirror.offline)
else
    echo 'apt-mirror-offline is not installed for Python 3.' >&2
    exit 69
fi

"${offline_cli[@]}" verify "$1" --feedback-dir "$2"
