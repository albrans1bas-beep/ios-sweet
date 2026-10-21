#!/usr/bin/env bash
# Build the QEMU iOS rootfs locally (WSL/Linux), apply SWEET tweaks and the
# UIKitCore DSC patch. Run as root inside WSL after installing hfsprogs,
# apfs-fuse, rsync and a kernel with CONFIG_HFSPLUS_FS=y.
#
#   local-build-image.sh OS.dmg SystemOS.dmg AppOS.dmg OUT.img [SIZE]
#
# The image must live on a Linux filesystem (not /mnt/*): loop mounts need it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

os=$(realpath "$1") sysos=$(realpath "$2") appos=$(realpath "$3") out=$4 size=${5:-15G}

"$here/mkiosroot.sh" "$os" "$sysos" "$appos" "$out" "$size"

mkdir -p /mnt/iosroot
fsck.hfsplus -fy "$out" || true
mount -t hfsplus -o loop,force "$out" /mnt/iosroot
trap 'umount /mnt/iosroot' EXIT

python3 "$here/sweet-root-tweaks.py" /mnt/iosroot
python3 "$here/patch-ios-dsc.py" /mnt/iosroot/System/Library/Caches/com.apple.dyld

umount /mnt/iosroot
trap - EXIT
fsck.hfsplus -fy "$out" || true
echo "image ready: $out"
