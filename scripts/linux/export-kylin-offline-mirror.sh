#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: export-kylin-offline-mirror.sh [--media-root DIR] [OPTIONS]

Options:
  --config FILE          apt-mirror config (default: /etc/apt/mirror-kylin.list)
  --mirror-root DIR      mirrored filesystem root (default: /var/spool/apt-mirror/mirror)
  --state-dir DIR        persistent ACK/hash state (default: /var/lib/apt-mirror-offline)
  --volume-size SIZE     optical payload limit such as 4300M (default: 0)
  --skip-online-sync     export the existing mirror without running apt-mirror
  --rehash-source        ignore the external source hash cache for this scan
  --hash-only            update the hash cache without creating outgoing data
EOF
}

mirror_config=/etc/apt/mirror-kylin.list
mirror_root=/var/spool/apt-mirror/mirror
state_dir=/var/lib/apt-mirror-offline
volume_size=0
media_root=
skip_online_sync=0
rehash_source=0
hash_only=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config|--mirror-root|--state-dir|--volume-size|--media-root)
            if [[ $# -lt 2 ]]; then
                echo "Missing value for $1" >&2
                usage >&2
                exit 64
            fi
            case "$1" in
                --config) mirror_config=$2 ;;
                --mirror-root) mirror_root=$2 ;;
                --state-dir) state_dir=$2 ;;
                --volume-size) volume_size=$2 ;;
                --media-root) media_root=$2 ;;
            esac
            shift 2
            ;;
        --skip-online-sync)
            skip_online_sync=1
            shift
            ;;
        --rehash-source)
            rehash_source=1
            shift
            ;;
        --hash-only)
            hash_only=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 64
            ;;
    esac
done

if [[ $hash_only -eq 0 && -z $media_root ]]; then
    echo '--media-root is required.' >&2
    usage >&2
    exit 64
fi
if [[ $hash_only -eq 0 && (! -d $media_root || ! -w $media_root) ]]; then
    echo "Transfer media is not a writable directory: $media_root" >&2
    exit 73
fi

if python3 -c 'import apt_mirror.offline' >/dev/null 2>&1; then
    offline_cli=(python3 -m apt_mirror.offline)
else
    echo 'The current apt-mirror-offline module is not installed for Python 3.' >&2
    exit 69
fi

if [[ $skip_online_sync -eq 0 ]]; then
    if [[ ! -f $mirror_config ]]; then
        echo "apt-mirror config does not exist: $mirror_config" >&2
        exit 66
    fi
    if python3 -c 'from apt_mirror.download.downloader import Downloader; assert Downloader.PARTIAL_DIRECTORY == ".apt-mirror2-partial"' >/dev/null 2>&1; then
        mirror_cli=(python3 -m apt_mirror)
    else
        echo 'The resume-enabled version of this project is not installed for Python 3.' >&2
        echo 'Install/update this checkout and its online dependencies before synchronizing.' >&2
        exit 69
    fi
    resume_notice() {
        sync || true
        echo 'Online synchronization did not finish; no offline bundle was created.' >&2
        echo 'Completed files and HTTP partial downloads were retained.' >&2
        echo 'Rerun this script with the same config and mirror paths to continue.' >&2
    }
    trap 'resume_notice; exit 130' INT
    trap 'resume_notice; exit 143' TERM
    echo 'Synchronizing archive.kylinos.cn (an interrupted rerun resumes HTTP partials)...'
    if "${mirror_cli[@]}" "$mirror_config"; then
        sync_status=0
    else
        sync_status=$?
    fi
    trap - INT TERM
    if [[ $sync_status -ne 0 ]]; then
        resume_notice
        exit "$sync_status"
    fi
fi

if [[ $hash_only -eq 1 ]]; then
    hash_arguments=(hash "$mirror_root" --state-dir "$state_dir")
    if [[ $rehash_source -eq 1 ]]; then
        hash_arguments+=(--rehash-source)
    fi
    echo 'Hashing mirror without creating outgoing data...'
    "${offline_cli[@]}" "${hash_arguments[@]}"
    sync
    echo "Hash cache updated: $state_dir/hash-cache.json"
    exit 0
fi

mkdir -p "$media_root/feedback" "$media_root/outgoing" "$state_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
bundle="$media_root/outgoing/bundle-$stamp-$$"
export_arguments=(
    export "$mirror_root" "$bundle"
    --state-dir "$state_dir"
    --feedback-dir "$media_root/feedback"
    --volume-size "$volume_size"
)
if [[ $rehash_source -eq 1 ]]; then
    export_arguments+=(--rehash-source)
fi

echo 'Building verified incremental bundle...'
"${offline_cli[@]}" "${export_arguments[@]}"
sync
echo "Bundle ready: $bundle"
if [[ $volume_size != 0 ]]; then
    echo 'For optical media, burn the contents of each volumes/volume-NNNN directory.'
fi
echo 'Unmount or eject the transfer filesystem cleanly before moving it.'
