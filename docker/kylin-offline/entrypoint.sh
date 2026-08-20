#!/usr/bin/env bash
set -Eeuo pipefail

declare -a mounted_points=()
declare -a connected_devices=()
declare -a created_partition_nodes=()
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
        local devices_clean=1
        for ((index=${#connected_devices[@]} - 1; index >= 0; index--)); do
            if ! qemu-nbd --disconnect "${connected_devices[index]}"; then
                log "WARNING: unable to disconnect ${connected_devices[index]}"
                devices_clean=0
                status=1
            fi
        done
        if [[ $devices_clean -eq 1 ]]; then
            for ((index=${#created_partition_nodes[@]} - 1; index >= 0; index--)); do
                rm -f -- "${created_partition_nodes[index]}"
            done
        else
            log 'WARNING: preserving partition device nodes because an NBD connection is still active'
        fi
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

create_partition_node() {
    local partition=$1
    local device=${partition%p*}
    local sysfs_name=${partition#/dev/}
    local dev_file="/sys/class/block/$sysfs_name/dev"
    local numbers=''
    local major
    local minor

    [[ -b $partition ]] && return 0
    [[ -e $partition ]] && return 1

    # In a privileged container, udev may not create the node even though the
    # kernel has exposed the partition in sysfs.  The sysfs dev file provides
    # the authoritative major:minor pair; lsblk is a fallback for systems that
    # expose the same information only through its block-device view.
    if [[ -r $dev_file ]]; then
        numbers=$(<"$dev_file")
    fi
    if [[ ! $numbers =~ ^[0-9]+:[0-9]+$ ]]; then
        numbers=$(lsblk -nrpo NAME,MAJ:MIN,TYPE "$device" 2>/dev/null \
            | awk -v partition="$partition" '$1 == partition && $3 == "part" { print $2; exit }' \
            || true)
    fi
    [[ $numbers =~ ^[0-9]+:[0-9]+$ ]] || return 1
    major=${numbers%%:*}
    minor=${numbers#*:}

    if mknod -- "$partition" b "$major" "$minor" 2>/dev/null; then
        created_partition_nodes+=("$partition")
        log "created missing partition device node $partition ($major:$minor)"
        return 0
    fi
    return 1
}

mount_vhdx() {
    local image=$1
    local mount_point=$2
    local device=$3
    local partition_spec=$4
    local read_only=$5
    local partition
    local pid_file="/sys/class/block/${device#/dev/}/pid"
    local -a connect_options=(--connect="$device" --format=vhdx)
    local mount_mode=rw
    local attempt

    validate_device "$device"
    if [[ $partition_spec =~ ^[1-9][0-9]*$ ]]; then
        # Keep accepting the historical partition-number form (for example,
        # `1`) while allowing callers to provide `/dev/nbd0p1` directly.
        partition="${device}p${partition_spec}"
    else
        [[ $partition_spec =~ ^/dev/nbd[0-9]+p[1-9][0-9]*$ ]] \
            || die "invalid partition path for $image: $partition_spec"
        [[ ${partition_spec%p*} == "$device" ]] \
            || die "partition $partition_spec does not belong to device $device"
        partition=$partition_spec
    fi
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
    blockdev --rereadpt "$device" 2>/dev/null || true
    udevadm settle 2>/dev/null || true

    for attempt in {1..50}; do
        [[ -b $partition ]] && break
        create_partition_node "$partition" || true
        [[ -b $partition ]] && break
        sleep 0.1
    done
    [[ -b $partition ]] || die "expected VHDX partition device was not found: $partition"

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
publish_root "$mirror_root"

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
