#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: with-kylin-offline-vhdx.sh VHDX MOUNT_POINT [NBD_DEVICE] -- COMMAND [ARG...]

Connects a Windows dynamic VHDX with qemu-nbd, mounts its first partition,
runs COMMAND, then syncs and disconnects it even if COMMAND fails.
EOF
}

if [[ $# -lt 4 ]]; then
    usage >&2
    exit 64
fi

vhdx=$1
mount_point=$2
shift 2
nbd_device=/dev/nbd0
if [[ $1 != -- ]]; then
    nbd_device=$1
    shift
fi
if [[ $1 != -- ]]; then
    usage >&2
    exit 64
fi
shift

if [[ $EUID -ne 0 ]]; then
    echo 'Run this script as root.' >&2
    exit 77
fi
if [[ ! -f $vhdx ]]; then
    echo "VHDX does not exist: $vhdx" >&2
    exit 66
fi
if [[ ! $nbd_device =~ ^/dev/nbd[0-9]+$ ]]; then
    echo "Unsafe NBD device name: $nbd_device" >&2
    exit 64
fi

partition=${nbd_device}p1
connected=0
mounted=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ $mounted -eq 1 ]]; then
        sync
        umount "$mount_point" || status=$?
        mounted=0
    fi
    if [[ $connected -eq 1 ]]; then
        qemu-nbd --disconnect "$nbd_device" || status=$?
        connected=0
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v qemu-nbd >/dev/null 2>&1 || {
    echo 'qemu-nbd is required (usually provided by qemu-utils).' >&2
    exit 69
}
modprobe nbd max_part=16
pid_file=/sys/class/block/${nbd_device#/dev/}/pid
if [[ -r $pid_file ]] && [[ -n $(<"$pid_file") ]]; then
    echo "$nbd_device is already connected." >&2
    exit 73
fi
if mountpoint -q "$mount_point"; then
    echo "Mount point is already in use: $mount_point" >&2
    exit 73
fi

mkdir -p "$mount_point"
qemu-nbd --connect="$nbd_device" --format=vhdx "$vhdx"
connected=1
udevadm settle || true
if [[ ! -b $partition ]]; then
    echo "Expected VHDX partition was not found: $partition" >&2
    exit 65
fi
mount -o rw "$partition" "$mount_point"
mounted=1

"$@"
