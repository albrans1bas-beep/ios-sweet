#!/bin/sh
# SWEET guest-side MobileGestalt probe.
#
# SpringBoard's main thread blocks for ~19 minutes in
# dispatch_semaphore_wait <- libMobileGestalt <- -[ISDefaults init] on every
# boot.  The kernel side shows nothing about it, so this script runs INSIDE
# iOS (as root, from a repurposed launchd boot-task job) and records what the
# gestalt side of the system actually looks like while SpringBoard is stuck:
# the SystemGroup container, the on-disk gestalt cache, and launchd's view of
# MobileGestaltHelper.  CI copies the file out of the image after the smoke
# run (`--- mgprobe ---`).
OUT=/private/var/log/mgprobe.txt
GESTDIR=/private/var/containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache
GESTPLIST=$GESTDIR/Library/Caches/com.apple.MobileGestalt.plist

mkdir -p /private/var/log
: > "$OUT"
log() { echo "--- $(date '+%Y-%m-%d %H:%M:%S') $*" >> "$OUT"; }
run() { echo "\$ $*" >> "$OUT"; "$@" >> "$OUT" 2>&1; echo >> "$OUT"; }

log "mgprobe start"
sleep 240

i=0
while [ $i -lt 10 ]; do
	log "iteration $i"
	echo "\$ ls -la /private/var/containers/Shared/SystemGroup" >> "$OUT"
	ls -la /private/var/containers/Shared/SystemGroup >> "$OUT" 2>&1
	echo >> "$OUT"
	echo "\$ find $GESTDIR (depth 4)" >> "$OUT"
	find "$GESTDIR" -maxdepth 4 -printf '%s %m %u %p\n' >> "$OUT" 2>&1
	echo >> "$OUT"
	if [ -f "$GESTPLIST" ]; then
		log "MG cache plist exists"
		run plutil -p "$GESTPLIST"
	else
		log "MG cache plist MISSING"
	fi
	echo "\$ launchctl list | grep -i gestalt" >> "$OUT"
	launchctl list 2>&1 | grep -i gestalt >> "$OUT" 2>&1
	echo >> "$OUT"
	echo "\$ launchctl print system/com.apple.mobilegestalt.xpc" >> "$OUT"
	launchctl print system/com.apple.mobilegestalt.xpc 2>&1 | head -40 >> "$OUT"
	echo >> "$OUT"
	echo "\$ launchctl print system/com.apple.containermanagerd (head)" >> "$OUT"
	launchctl print system/com.apple.containermanagerd 2>&1 | head -25 >> "$OUT"
	echo >> "$OUT"
	for p in /private/var/mobile /private/var/containers/Bundle/Application; do
		echo "\$ ls -la $p" >> "$OUT"
		ls -la "$p" >> "$OUT" 2>&1 | head -10
		echo >> "$OUT"
	done
	i=$((i + 1))
	sleep 60
done
log "mgprobe done"
