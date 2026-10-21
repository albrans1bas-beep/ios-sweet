#!/usr/bin/env python3
"""Patch -[UIScreen _boundingPathUtilities] in an iOS 16 split dyld cache.

Usage: patch-ios-dsc.py <dir-with-dyld_shared_cache_arm64*>

Returns nil from the getter so UIKitCore takes the pre-notch (no complex
corner-radius) path and never loads BoundingPathBitmap assets. The entry
comes from `ipsw dyld a2s` on the crash stack (0x18908963c - +40).
Patches in place; prints the file and offsets; fails unless the target
starts with a standard arm64 prologue.
"""
import glob
import struct
import sys

VADDR = 0x189089614
PATCH = struct.pack('<II', 0x52800000, 0xd65f03c0)  # mov w0,#0 ; ret


def main():
    cdir = sys.argv[1]
    hit = None
    for path in sorted(glob.glob(cdir + '/dyld_shared_cache_arm64*')):
        try:
            f = open(path, 'r+b')
        except IsADirectoryError:
            continue
        hdr = f.read(0x4000)
        if len(hdr) < 0x1a0 or hdr[:7] != b'dyld_v1':
            print('skip', path, hdr[:16])
            f.close()
            continue
        mapping_offset, mapping_count = struct.unpack_from('<II', hdr, 16)
        f.seek(0, 2)
        fsize = f.tell()
        print(path, 'size', fsize, 'mappings at', mapping_offset, 'count', mapping_count)
        for i in range(mapping_count):
            vm, sz = struct.unpack_from('<QQ', hdr, mapping_offset + i * 32)
            fo = struct.unpack_from('<I', hdr, mapping_offset + i * 32 + 16)[0]
            ok = sz > 0 and fo + sz <= fsize
            print('  mapping', i, hex(vm), hex(sz), hex(fo), '' if ok else 'INVALID')
            if ok and vm <= VADDR < vm + sz and hit is None:
                hit = (path, f, fsize, fo + (VADDR - vm))
        if hit is None or hit[0] != path:
            f.close()
    assert hit is not None, 'no cache mapping covers the vaddr'
    path, f, fsize, foff = hit
    print('patching', path, 'at', hex(foff))
    assert foff + 8 <= fsize, 'patch outside cache file'
    f.seek(foff)
    old = f.read(8)
    print('old bytes:', old.hex())
    w = struct.unpack('<I', old[:4])[0]
    if w in (0x52800000, 0xd65f03c0):
        print('already patched')
    elif (w & 0xffc00000) == 0xa9800000 or w == 0xd503237f:
        f.seek(foff)
        f.write(PATCH)
        f.seek(foff)
        nb = f.read(8)
        print('new bytes:', nb.hex())
        assert nb == PATCH
        print('PATCHED UIScreen _boundingPathUtilities -> mov w0,#0; ret in', path)
    else:
        print('first word', hex(w), 'is not a known prologue; refusing')
        raise SystemExit(1)
    f.close()


if __name__ == '__main__':
    main()
