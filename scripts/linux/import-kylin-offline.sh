#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: import-kylin-offline.sh BUNDLE MIRROR_ROOT FEEDBACK_DIR [DELETE_POLICY]

DELETE_POLICY is prompt (default), report, or apply.  Exit code 3 means that
upstream deletions were reported but have not yet been applied.
EOF
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
    usage >&2
    exit 64
fi

bundle=$1
mirror_root=$2
feedback_dir=$3
delete_policy=${4:-prompt}

case "$delete_policy" in
    prompt|report|apply) ;;
    *)
        echo "Invalid delete policy: $delete_policy" >&2
        exit 64
        ;;
esac

if command -v apt-mirror-offline >/dev/null 2>&1; then
    offline_cli=(apt-mirror-offline)
elif python3 -c 'import apt_mirror.offline' >/dev/null 2>&1; then
    offline_cli=(python3 -m apt_mirror.offline)
else
    echo 'apt-mirror-offline is not installed for root/Python 3.' >&2
    exit 69
fi

set +e
"${offline_cli[@]}" import "$bundle" "$mirror_root" \
    --feedback-dir "$feedback_dir" --delete-policy "$delete_policy"
status=$?
set -e

if [[ $status -eq 3 ]]; then
    echo "Deletion review is pending. Review $feedback_dir/deletions-pending.json." >&2
    echo "Rerun with DELETE_POLICY=apply to accept, or keep report mode." >&2
fi
exit "$status"
