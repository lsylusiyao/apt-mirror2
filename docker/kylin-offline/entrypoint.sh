#!/usr/bin/env bash
set -Eeuo pipefail

declare -a mounted_points=()
declare -a connected_devices=()
child_pid=

log() {
    printf '[kylin-container] %s\n' "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

cleanup() {
    local status=$?
    local index
    local mounts_clean=1
    trap - EXIT INT TERM

    for ((index=${#mounted_points[@]} - 1; index >= 0; index--)); do
        sync || true
        if ! umount "${mounted_points[index]}"; then
            log "WARNING: unable to unmount ${mounted_points[index]}"
            mounts_clean=0
            status=1
        fi
    done
    if [[ $mounts_clean -eq 1 ]]; then
        for ((index=${#connected_devices[@]} - 1; index >= 0; index--)); do
            if ! qemu-nbd --disconnect "${connected_devices[index]}"; then
                log "WARNING: unable to disconnect ${connected_devices[index]}"
                status=1
            fi
        done
    else
        log 'WARNING: preserving NBD connections because a filesystem is still mounted'
    fi
    exit "$status"
}

forward_signal() {
    local signal=$1
    local status=$2
    if [[ -n ${child_pid:-} ]]; then
        kill "-$signal" "$child_pid" 2>/dev/null || true
    else
        exit "$status"
    fi
}

trap cleanup EXIT
trap 'forward_signal TERM 143' TERM
trap 'forward_signal INT 130' INT

validate_device() {
    local device=$1
    [[ $device =~ ^/dev/nbd[0-9]+$ ]] || die "unsafe NBD device name: $device"
}

mount_vhdx() {
    local image=$1
    local mount_point=$2
    local device=$3
    local partition_number=$4
    local read_only=$5
    local partition="${device}p${partition_number}"
    local pid_file="/sys/class/block/${device#/dev/}/pid"
    local -a connect_options=(--connect="$device" --format=vhdx)
    local mount_mode=rw
    local attempt

    validate_device "$device"
    [[ $partition_number =~ ^[1-9][0-9]*$ ]] \
        || die "invalid partition number for $image: $partition_number"
    [[ -f $image ]] || die "VHDX file does not exist: $image"
    [[ -s $image ]] || die "VHDX file is empty (was the host file mounted?): $image"
    [[ $EUID -eq 0 ]] || die 'VHDX mode must run as root'

    modprobe nbd max_part=16 2>/dev/null \
        || [[ -b $device ]] \
        || die 'cannot load the host nbd module; load it on the Docker host first'
    [[ -b $device ]] \
        || die "$device is unavailable; load nbd before starting the container"
    if [[ -r $pid_file && -n $(<"$pid_file") ]]; then
        die "$device is already connected"
    fi
    if mountpoint -q "$mount_point"; then
        die "mount point is already in use: $mount_point"
    fi

    if [[ $read_only == 1 ]]; then
        connect_options+=(--read-only)
        mount_mode=ro
    elif [[ $read_only != 0 ]]; then
        die "read-only setting must be 0 or 1, got: $read_only"
    fi

    mkdir -p "$mount_point"
    qemu-nbd "${connect_options[@]}" "$image"
    connected_devices+=("$device")
    partprobe "$device" 2>/dev/null || true
    udevadm settle 2>/dev/null || true

    for attempt in {1..50}; do
        [[ -b $partition ]] && break
        sleep 0.1
    done
    [[ -b $partition ]] || die "expected VHDX partition was not found: $partition"

    mount -o "$mount_mode" "$partition" "$mount_point"
    mounted_points+=("$mount_point")
    log "mounted $image on $mount_point ($mount_mode, $device)"
}

publish_root() {
    local requested_root=$1
    local resolved_root
    local temporary_link=/var/www/.kylin-mirror.tmp

    [[ -d $requested_root ]] || die "mirror root is not a directory: $requested_root"
    resolved_root=$(realpath -- "$requested_root")
    rm -f -- "$temporary_link"
    ln -s -- "$resolved_root" "$temporary_link"
    mv -Tf -- "$temporary_link" /var/www/kylin-mirror
    log "nginx is publishing $resolved_root"
}

if [[ -n ${KYLIN_MIRROR_VHDX:-} ]]; then
    mount_vhdx \
        "$KYLIN_MIRROR_VHDX" \
        "$KYLIN_MIRROR_MOUNT" \
        "$KYLIN_MIRROR_NBD" \
        "$KYLIN_MIRROR_PARTITION" \
        "${KYLIN_MIRROR_READ_ONLY:-0}"

    if [[ $KYLIN_MIRROR_SUBDIR == /* ]]; then
        die 'KYLIN_MIRROR_SUBDIR must be relative to the VHDX filesystem root'
    fi
    mirror_root=$(realpath -m -- "$KYLIN_MIRROR_MOUNT/$KYLIN_MIRROR_SUBDIR")
    case "$mirror_root" in
        "$KYLIN_MIRROR_MOUNT"|"$KYLIN_MIRROR_MOUNT"/*) ;;
        *) die 'KYLIN_MIRROR_SUBDIR escapes the VHDX mount point' ;;
    esac
    if [[ ! -d $mirror_root && ${KYLIN_MIRROR_READ_ONLY:-0} == 0 ]]; then
        mkdir -p "$mirror_root"
    fi
else
    mirror_root=$KYLIN_MIRROR_ROOT
    mkdir -p "$mirror_root"
fi

if [[ -n ${KYLIN_TRANSFER_VHDX:-} ]]; then
    mount_vhdx \
        "$KYLIN_TRANSFER_VHDX" \
        "$KYLIN_TRANSFER_MOUNT" \
        "$KYLIN_TRANSFER_NBD" \
        "$KYLIN_TRANSFER_PARTITION" \
        "${KYLIN_TRANSFER_READ_ONLY:-0}"
fi

[[ -d $mirror_root ]] || die "mirror root is not a directory: $mirror_root"
mirror_root=$(realpath -- "$mirror_root")
if [[ ${KYLIN_PUBLIC_SUBDIR:-} == /* ]]; then
    die 'KYLIN_PUBLIC_SUBDIR must be relative to the mirror root'
fi
public_root=$(realpath -m -- "$mirror_root/${KYLIN_PUBLIC_SUBDIR:-.}")
case "$public_root" in
    "$mirror_root"|"$mirror_root"/*) ;;
    *) die 'KYLIN_PUBLIC_SUBDIR escapes the mirror root' ;;
esac
if [[ ! -d $public_root ]]; then
    mkdir -p "$public_root" \
        || die "published mirror directory does not exist and cannot be created: $public_root"
fi

publish_root "$public_root"

if [[ $# -eq 0 || $1 == serve ]]; then
    [[ $# -le 1 ]] || die "serve does not accept arguments: ${*:2}"
    nginx -t
    nginx -g 'daemon off;' &
    child_pid=$!
    set +e
    wait "$child_pid"
    status=$?
    set -e
    child_pid=
    exit "$status"
fi

"$@" &
child_pid=$!
set +e
wait "$child_pid"
status=$?
set -e
child_pid=
exit "$status"
