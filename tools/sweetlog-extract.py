#!/usr/bin/env python3
"""Reconstruct the SWEET console log from a QEMU pmemsave of the kernel ring.

qemu-sweet.py --dump-symbol sweet_log_ring:0x400000 --dump-symbol sweet_log_total:8
writes sweet_log_ring.bin (the 4 MiB ring, patch 0047 sweet_ufs.c) and
sweet_log_total.bin (the u64 byte counter). ring byte n lives at n % size,
so the valid span is the last min(total, size) bytes ending at total.

  sweetlog-extract.py RING.bin TOTAL.bin [OUT.txt]

Prints the log to stdout when OUT.txt is omitted."""
import struct
import sys


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    ring = open(sys.argv[1], 'rb').read()
    total = struct.unpack('<Q', open(sys.argv[2], 'rb').read(8))[0]
    size = len(ring)
    span = min(total, size)
    start = (total - span) % size
    data = ring[start:start + span]
    if len(data) < span:
        data += ring[:span - len(data)]
    text = data.decode('utf-8', 'replace')
    if len(sys.argv) == 4:
        open(sys.argv[3], 'w', encoding='utf-8').write(text)
        print(f'{sys.argv[3]}: {span} bytes from ring offset {start} (total {total})',
              file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
