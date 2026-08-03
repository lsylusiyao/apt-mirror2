#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: new-kylin-offline-vhdx.sh --path FILE [OPTIONS]

Creates a dynamically allocated VHDX with one GPT/exFAT partition.

Options:
  --path FILE            New .vhdx file (required; existing files are refused)
  --maximum-size SIZE    Virtual capacity accepted by qemu-img (default: 2T)
  --nbd-device DEVICE    Temporary NBD device (default: /dev/nbd0)
  --label LABEL          exFAT volume label, up to 15 characters
                         (default: KYLIN_OFFLINE)
EOF
}

image=
maximum_size=2T
nbd_device=/dev/nbd0
label=KYLIN_OFFLINE

while [[ $# -gt 0 ]]; do
    case "$1" in
        --path|--maximum-size|--nbd-device|--label)
            if [[ $# -lt 2 ]]; then
                echo "Missing value for $1" >&2
                usage >&2
                exit 64
            fi
            case "$1" in
                --path) image=$2 ;;
                --maximum-size) maximum_size=$2 ;;
                --nbd-device) nbd_device=$2 ;;
                --label) label=$2 ;;
            esac
            shift 2
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

if [[ -z $image ]]; then
    echo '--path is required.' >&2
    usage >&2
    exit 64
fi
if [[ ${image,,} != *.vhdx ]]; then
    echo "The output filename must end in .vhdx: $image" >&2
    exit 64
fi
if [[ ! $nbd_device =~ ^/dev/nbd[0-9]+$ ]]; then
    echo "Unsafe NBD device name: $nbd_device" >&2
    exit 64
fi
if [[ ! $label =~ ^[A-Za-z0-9_-]{1,15}$ ]]; then
    echo 'The label must contain 1-15 ASCII letters, digits, underscores, or hyphens.' >&2
    exit 64
fi
if [[ $EUID -ne 0 ]]; then
    echo 'Run this script as root.' >&2
    exit 77
fi

parent=$(dirname -- "$image")
if [[ ! -d $parent ]]; then
    echo "Parent directory does not exist: $parent" >&2
    exit 66
fi
if [[ -e $image ]]; then
    echo "Refusing to overwrite an existing path: $image" >&2
    exit 73
fi

for required_command in qemu-img qemu-nbd parted partprobe mkfs.exfat modprobe udevadm; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "Required command was not found: $required_command" >&2
        exit 69
    fi
done

modprobe nbd max_part=16
pid_file=/sys/class/block/${nbd_device#/dev/}/pid
if [[ -r $pid_file ]] && [[ -n $(<"$pid_file") ]]; then
    echo "$nbd_device is already connected." >&2
    exit 73
fi

partition=${nbd_device}p1
created=0
connected=0
initialized=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ $connected -eq 1 ]]; then
        if qemu-nbd --disconnect "$nbd_device"; then
            connected=0
        else
            status=$?
            echo "Could not disconnect $nbd_device; the new VHDX was preserved." >&2
        fi
    fi
    if [[ $status -ne 0 && $created -eq 1 && $initialized -eq 0 && $connected -eq 0 ]]; then
        rm -f -- "$image"
        echo "Removed incomplete VHDX after failure: $image" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

qemu-img create -f vhdx -o subformat=dynamic "$image" "$maximum_size"
created=1
qemu-nbd --connect="$nbd_device" --format=vhdx "$image"
connected=1
parted -s "$nbd_device" mklabel gpt mkpart primary 1MiB 100%
partprobe "$nbd_device"
udevadm settle
if [[ ! -b $partition ]]; then
    echo "Expected VHDX partition was not found: $partition" >&2
    exit 65
fi
mkfs.exfat -n "$label" "$partition"
sync
initialized=1
qemu-nbd --disconnect "$nbd_device"
connected=0

echo "Created dynamic VHDX: $image"
echo "Virtual capacity: $maximum_size; filesystem: exFAT; label: $label"
echo 'The host file grows as blocks are allocated, up to its virtual capacity.'
