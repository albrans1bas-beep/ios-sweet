#!/bin/bash
# Build the SWEET iOS root: one case-sensitive HFS+ image holding an iOS OS
# root filesystem plus its cryptexes, for the userdata partition (rd=ufsroot0).
#
#   mkiosroot.sh OS.dmg SYSTEMOS.dmg APPOS.dmg OUT.img [SIZE]     (SIZE default 16G)
#
# The IPSW images are raw APFS with LZBITMAP-compressed files: they are read
# through apfs-fuse (decompressing), so the HFS+ copy is uncompressed. On a
# device /private/preboot is its own volume holding the cryptexes, which
# /System/Cryptexes/{OS,App} link to; here they live inside the one volume.
# Owners, modes, symlinks and hard links are preserved (rsync -aH as root).
#
# The volume is journaled (XNU replays the journal after a panic; Linux only
# mounts it read-write with -o force and leaves the journal empty). SWEET
# additions: /System/Library/Caches/com.apple.dyld hard-links the SystemOS
# cryptex shared cache (ignition_level=1 looks there), and
# /private/var/keybags/systembag.kb is the plain-plist system keybag that
# keybagd reads with /defaults/no-effaceable-storage (tools/mkadt.py).
set -euo pipefail

[[ $# -ge 4 ]] || { sed -n '2,12p' "$0"; exit 2; }
os=$(realpath "$1") sysos=$(realpath "$2") appos=$(realpath "$3") out=$4 size=${5:-16G}
# A block device (QEMU -drive) is formatted in place instead of a loop image.
on_device=0
if [[ -b $out ]]; then
	on_device=1
else
	[[ ! -e $out ]] || { echo "refusing to overwrite $out" >&2; exit 1; }
fi

work=$(mktemp -d)
mounts=()
cleanup() {
	local busy=0
	for m in "${mounts[@]}"; do
		if mountpoint -q "$m"; then
			sudo umount "$m" 2>/dev/null || true
		fi
		mountpoint -q "$m" && busy=1
	done
	# Never recurse into a directory that is still mounted (the root is writable).
	if [[ $busy == 0 ]]; then
		rm -f "$work"/*.dmg
		rmdir "$work"/* "$work" 2>/dev/null || true
	else
		echo "still mounted under $work: left in place" >&2
	fi
}
trap cleanup EXIT

# apfs-fuse splits its options on ',' (e.g. in iPhone10,3 paths): use links.
# Keep the .dmg name: apfs-fuse only parses UDIF images (the OS one is UDIF,
# the cryptexes are raw APFS) when the file name ends in .dmg.
apfs_mount() {
	ln -s "$1" "$work/$2.dmg"
	mkdir "$work/$2"
	# As root: a user's FUSE mount is unreadable to the root rsync below.
	sudo "$(command -v apfs-fuse)" "$work/$2.dmg" "$work/$2"
	mounts+=("$work/$2")
}
apfs_mount "$os" os
apfs_mount "$sysos" sysos
apfs_mount "$appos" appos

if ((on_device)); then
	mkfs.hfsplus -s -v iOSRoot "$out"
else
	truncate -s "$size" "$out"
	mkfs.hfsplus -s -v iOSRoot "$out"
fi
mkdir "$work/root"
if ((on_device)); then
	sudo mount -t hfsplus -o force "$out" "$work/root"
else
	sudo mount -t hfsplus -o loop,force "$out" "$work/root"
fi
mounts=("$work/root" "${mounts[@]}")

copy() {
	echo "copying $1 -> ${2#"$work/root"}"
	sudo mkdir -p "$2"
	sudo rsync -aH --numeric-ids "$1/" "$2/"
}
copy "$work/os/root" "$work/root"
copy "$work/sysos/root" "$work/root/private/preboot/Cryptexes/OS"
copy "$work/appos/root" "$work/root/private/preboot/Cryptexes/App"

# Shared cache where dyld looks at ignition level 1: hard links, no extra space.
dsc=private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld
sudo mkdir -p "$work/root/System/Library/Caches/com.apple.dyld"
for f in "$work/root/$dsc"/*; do
	sudo ln "$f" "$work/root/System/Library/Caches/com.apple.dyld/${f##*/}"
done

# dyld does not search the cryptexes at ignition level 1: hard-link every
# cryptex file that has no counterpart in the OS root (libobjc-trampolines,
# libffi-trampolines, libstdc++, ...) to its root path.
for c in OS App; do
	(cd "$work/root/private/preboot/Cryptexes/$c" && sudo find . ! -type d ! -path './System/Library/Caches/com.apple.dyld/*') |
	while IFS= read -r f; do
		f=${f#./}
		if ! sudo test -e "$work/root/$f" && ! sudo test -L "$work/root/$f"; then
			sudo mkdir -p "$work/root/$(dirname "$f")"
			src="$work/root/private/preboot/Cryptexes/$c/$f"
			# Linux hfsplus cannot hard-link symlinks: copy those.
			if sudo test -L "$src"; then sudo cp -P "$src" "$work/root/$f"; else sudo ln "$src" "$work/root/$f"; fi
			echo "linked $c:/$f"
		fi
	done
done

# DIAG: UIKit's bounding-path data, original vs the HFS+ copy (sizes/md5/mode).
for f in \
    System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/Assets.car \
    System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/BoundingPathData \
    System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/Info.plist \
    System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/_CodeSignature/CodeResources \
    System/Library/PrivateFrameworks/UIKitCore.framework/Artwork.bundle/Assets.car; do
    echo "DIAG $f"
    sudo md5sum "$work/os/root/$f" "$work/root/$f" 2>&1 || true
    sudo stat -c '%a %s %n' "$work/os/root/$f" "$work/root/$f" 2>&1 || true
    file "$work/root/$f" 2>/dev/null || true
done

# logd's log stores, normally made at restore: it checks owner _logd (272)
# and mode, plus a .migrated marker, else asks a helper and crashes
# ("BUG IN LIBTRACE: Failed to migrate directory").
db="$work/root/private/var/db"
sudo install -d -o 272 -g 272 -m 0751 "$db/diagnostics"
sudo install -d -o 272 -g 272 -m 0755 "$db/uuidtext" "$db/diagnostics/timesync"
for d in diagnostics uuidtext diagnostics/timesync; do
	sudo install -o 272 -g 272 -m 0644 /dev/null "$db/$d/.migrated"
done

# System keybag for the built-in AppleKeyStore (no passcode).
sudo install -d -m 0700 -o 0 -g 0 "$work/root/private/var/keybags"
printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
	'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	'<plist version="1.0"><dict><key>KeyBagKeys</key><data>U1dFRVQ=</data></dict></plist>' |
	sudo install -m 0600 -o 0 -g 0 /dev/stdin "$work/root/private/var/keybags/systembag.kb"

# First-boot LaunchServices seeding creates one data container per application
# bundle through a synchronous containermanagerd XPC; under TCG that runs at
# roughly one bundle per 7 seconds.  Keep the bundle set that SpringBoard needs
# for first boot and move the rest aside, so the seed scan (and therefore the
# long lsd startup) is short enough to observe the UI.  The full set can be
# restored by moving Applications.disabled entries back.
keep_apps=" Preferences.app Setup.app MobileSafari.app PosterBoard.app "
if sudo test -d "$work/root/Applications"; then
	sudo install -d "$work/root/Applications.disabled"
	for app in "$work/root/Applications"/*.app; do
		sudo test -e "$app" || continue
		case "$keep_apps" in
		*" ${app##*/} "*) continue ;;
		esac
		sudo mv "$app" "$work/root/Applications.disabled/"
	done
	echo "pruned applications; kept:"
	sudo ls "$work/root/Applications" | head -20
fi

# lsd's LaunchDaemon is demand-started by the first LaunchServices client in
# this userland (SpringBoard); its startup journal recovery then deadlocks
# with the queued blocking database fetch (lsd main thread waits in
# dispatch_sync while a worker waits for seeding, SpringBoard waits for lsd).
# Start it at boot so it seeds before any client asks, and drop the factory
# launchd job cache so the edited plist is read.
lsd_plist="$work/root/System/Library/LaunchDaemons/com.apple.lsd.plist"
if sudo test -f "$lsd_plist"; then
	sudo python3 - "$lsd_plist" <<'PY'
import plistlib, sys
p = sys.argv[1]
with open(p, 'rb') as f:
	d = plistlib.load(f)
print('lsd plist before:', d)
d['RunAtLoad'] = True
with open(p, 'wb') as f:
	plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
print('lsd plist after:', d)
PY
else
	echo "no lsd plist at $lsd_plist; candidates:"
	sudo find "$work/root" -name '*lsd*.plist' | head
fi
# containermanagerd is demand-started by SpringBoard's first container
# lookup, and that first lookup arrives while the daemon is still
# initializing: it answers nil, SpringBoard aborts on
# -[NSURL initFileURLWithPath:] and launchd relaunches it (the second
# instance finds the daemon ready and reaches the lock screen).  Start it
# at boot like lsd, in both the plist and the compiled launchd cache.
for cm_plist in "$work/root/System/Library/LaunchDaemons/"*containermanager*.plist; do
	sudo test -f "$cm_plist" || continue
	echo "containermanagerd plist: $cm_plist"
	sudo python3 - "$cm_plist" <<'PYEOF'
import plistlib, sys
p = sys.argv[1]
with open(p, 'rb') as f:
	d = plistlib.load(f)
print('containermanagerd plist before: RunAtLoad =', d.get('RunAtLoad'))
d['RunAtLoad'] = True
with open(p, 'wb') as f:
	plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
print('containermanagerd plist after: RunAtLoad =', d.get('RunAtLoad'))
PYEOF
done

# launchd takes its job set from the compiled cache, not the LaunchDaemons
# plists (launchd_cache_loader -> /System/Library/xpc/launchd.plist,
# "Using unsecure cache"). Mark lsd as a boot job there too, otherwise the
# per-daemon edit above is never read.
launchd_cache="$work/root/System/Library/xpc/launchd.plist"
if sudo test -f "$launchd_cache"; then
	sudo python3 - "$launchd_cache" <<'PY'
import plistlib, sys

def find(o):
	if isinstance(o, dict):
		if o.get('Label') == 'com.apple.lsd':
			return o
		for v in o.values():
			r = find(v)
			if r is not None:
				return r
	elif isinstance(o, list):
		for v in o:
			r = find(v)
			if r is not None:
				return r
	return None

p = sys.argv[1]
with open(p, 'rb') as f:
	d = plistlib.load(f)
def find_all(o, out):
	if isinstance(o, dict):
		lbl = o.get('Label')
		if isinstance(lbl, str) and ('lsd' == lbl or 'containermanagerd' in lbl):
			out.append(o)
		for v in o.values():
			find_all(v, out)
	elif isinstance(o, list):
		for v in o:
			find_all(v, out)

jobs = []
find_all(d, jobs)
if not jobs:
	print('no lsd/containermanagerd job in compiled launchd cache')
	sys.exit(0)
for j in jobs:
	print('compiled job before:', j.get('Label'), 'RunAtLoad =', j.get('RunAtLoad'))
	j['RunAtLoad'] = True
with open(p, 'wb') as f:
	plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
for j in jobs:
	print('compiled job after:', j.get('Label'), 'RunAtLoad =', j.get('RunAtLoad'))
if not jobs:
	print('no lsd/containermanagerd job in compiled launchd cache')
	sys.exit(0)
for j in jobs:
	print('compiled job before:', j.get('Label'), 'RunAtLoad =', j.get('RunAtLoad'))
	j['RunAtLoad'] = True
with open(p, 'wb') as f:
	plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
for j in jobs:
	print('compiled job after:', j.get('Label'), 'RunAtLoad =', j.get('RunAtLoad'))
PY
else
	echo "no compiled launchd cache at $launchd_cache"
fi

sudo find "$work/root/private/var/db" -maxdepth 2 -iname '*launchd*' -print 2>/dev/null || true
sudo rm -rf "$work/root/private/var/db/com.apple.xpc.launchd" \
	"$work/root/private/var/db/launchd.db"

df -h "$work/root"
sudo umount "$work/root"
# Linux hfsplus decomposes some non-ASCII names (localized Shortcuts examples)
# differently from Apple's tables; let fsck_hfs rewrite them, then verify.
fsck.hfsplus -f -y "$out" | grep -v 'Illegal name' || true
fsck.hfsplus -f -n "$out"
