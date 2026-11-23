#!/usr/bin/env python3
"""Boot a SWEETXNU payload (tools/mkpayload.py) in QEMU's virt board.

  qemu-sweet.py PAYLOAD OUTDIR [--secs N] [--gdb]

Does what shim/loader.c does on the phone, but on the host: places the
kernel segments, ADT and boot_args at their physical addresses and starts
the CPU at a 6-instruction stub that jumps to the entry with x0 = boot_args.
virt RAM starts at 0x40000000; with 4G it covers the phone's framebuffer
(0x9c000000) and the kernel's phys_base (0xb0000000) as plain memory.

After N seconds (or when the guest stops) it writes into OUTDIR:
  fb.png     the 1080x2400 framebuffer (what the phone screen would show)
  regs.txt   QEMU's view of CPU state
  qemu.log   exceptions with ESR/FAR/ELR (-d int,guest_errors)

With --kernel KERNEL (the matching unstripped Mach-O), also decode the
framebuffer using its iso_font into fb.txt and write result.json. This is a
snapshot of the circular screen, not a complete boot log. --fail-on-panic
returns 2 when a panic is visible; absence of a panic does not prove boot.
An undecodable or blank screen returns 3 with --fail-on-panic.
"""
import argparse, json, os, shutil, socket, struct, subprocess, sys, time, zlib

FB_BASE, FB_W, FB_H = 0x9c000000, 1080, 2400
FB_STRIDE = FB_W * 4
PAGE = 0x4000
STUB_PA = 0x40200000	# above the dtb QEMU places at RAM base
BOOT_LINE_LENGTH = 608


def build_image(payload):
    """Return (phys_base, ram_image, boot_args_pa, entry_pa, virt_base)."""
    magic, ver, nsegs = struct.unpack_from('<8sII', payload, 0)
    assert magic == b'SWEETXNU' and ver == 1, 'not a SWEETXNU v1 payload'
    virt_base, entry_va, adt_off, adt_size, kernel_end, phys_base, mem_size = \
        struct.unpack_from('<7Q', payload, 0x10)
    cmdline = payload[0x48:0x48 + BOOT_LINE_LENGTH]
    delta = phys_base - virt_base
    adt_pa = kernel_end + delta
    ba_pa = (adt_pa + adt_size + PAGE - 1) & ~(PAGE - 1)
    top = ba_pa + PAGE
    img = bytearray(top - phys_base)
    for i in range(nsegs):
        vmaddr, vmsize, foff, fsize = struct.unpack_from('<4Q', payload, 0x2a8 + 32 * i)
        o = vmaddr + delta - phys_base
        img[o:o + fsize] = payload[foff:foff + fsize]
    img[adt_pa - phys_base:adt_pa - phys_base + adt_size] = payload[adt_off:adt_off + adt_size]
    ba = struct.pack('<HH4x4Q6QI4xQI', 2, 2, virt_base, phys_base, mem_size, top,
                     FB_BASE, 1, FB_STRIDE, FB_W, FB_H, 32, 0, adt_pa - delta, adt_size)
    ba += cmdline + b'\0' * 4 + struct.pack('<QQ', 0, mem_size)
    assert len(ba) == 736
    img[ba_pa - phys_base:ba_pa - phys_base + len(ba)] = ba
    return phys_base, bytes(img), ba_pa, entry_va + delta, virt_base


def stub(ba_pa, entry_pa):
    # ldr x0, =ba ; ldr x1, =entry ; br x1 ; nop ; .quad ba ; .quad entry
    return struct.pack('<4I2Q', 0x58000080, 0x580000a1, 0xd61f0020, 0xd503201f, ba_pa, entry_pa)


def write_png(path, raw):
    rows = []
    for y in range(FB_H):
        line = raw[y * FB_STRIDE:(y + 1) * FB_STRIDE]
        rgb = bytearray(FB_W * 3)	# a8r8g8b8 little endian = B G R A in memory
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        rows.append(b'\0' + bytes(rgb))
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', FB_W, FB_H, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


def payload_data(payload, va, size):
    """Read initialized segment bytes by VA (the payload has no slide)."""
    nsegs = struct.unpack_from('<I', payload, 12)[0]
    for i in range(nsegs):
        vmaddr, _, foff, fsize = struct.unpack_from('<4Q', payload, 0x2a8 + 32 * i)
        if vmaddr <= va and va + size <= vmaddr + fsize:
            offset = foff + va - vmaddr
            return payload[offset:offset + size]
    raise ValueError(f'VA {va:#x}+{size:#x} not in initialized payload segments')


def decode_screen(raw, font):
    """Exact glyph matching for pe_serial.c's 2x, white-on-black fbcon."""
    glyphs = {font[c * 16:(c + 1) * 16]: chr(c) for c in range(32, 127)}
    rows, unknown = [], 0
    for row in range(FB_H // 32):
        chars = []
        for col in range(FB_W // 16):
            glyph = bytearray(16)
            for y in range(16):
                offset = (row * 32 + y * 2) * FB_STRIDE + col * 16 * 4
                for x in range(8):
                    if raw[offset + x * 8:offset + x * 8 + 4] == b'\xff' * 4:
                        glyph[y] |= 1 << x
            key = bytes(glyph)
            char = glyphs.get(key, '?')
            unknown += key not in glyphs
            chars.append(char)
        rows.append(''.join(chars).rstrip())
    return '\n'.join(rows).rstrip() + '\n', unknown


def screen_result(screen, unknown):
    if 'panic(cpu ' in screen or 'SWEET EARLY PANIC:' in screen:
        status = 'panic'
    elif unknown:
        status = 'incomplete_decode'
    elif screen.strip():
        status = 'no_panic_observed'
    else:
        status = 'blank_screen'
    return {'status': status, 'unknown_glyphs': unknown, 'snapshot_only': True}


def qmp(sock_path, *cmds, pmemsaves=()):
    """Run QEMU human-monitor commands over QMP (synchronous, unlike the echoing HMP socket).

    POSIX: unix:... socket at sock_path. Windows has no AF_UNIX bridging in all
    QEMU builds, so main() picks a free TCP port and stores ';<port>' in
    sock_path; we then connect to 127.0.0.1:<port>."""
    if os.name == 'nt' and ';' in sock_path:
        s = socket.create_connection(('127.0.0.1', int(sock_path.split(';')[1])))
    else:
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock_path)
    f = s.makefile('rw')
    f.readline()

    def call(execute, **args):
        f.write(json.dumps({'execute': execute, 'arguments': args}) + '\n')
        f.flush()
        while True:
            r = json.loads(f.readline())
            if 'return' in r or 'error' in r:
                return r

    call('qmp_capabilities')
    out = []
    for c in cmds:
        r = call('human-monitor-command', **{'command-line': c})
        if 'error' in r:
            print(f'qmp {c!r}: {r["error"]}', file=sys.stderr)
        out.append(r.get('return', ''))
    for addr, size, filename in pmemsaves:	# native QMP: HMP's argument parser chokes on '-' in paths
        r = call('pmemsave', val=addr, size=size, filename=filename)
        if 'error' in r:
            print(f'qmp pmemsave {filename}: {r["error"]}', file=sys.stderr)
    s.close()
    return out


def macho_symbols(path):
    """Defined symbols of a little-endian Mach-O, for hosts without llvm-nm.

    Returns {name: vmaddr}; names keep their leading underscore like nm output.
    """
    data = open(path, 'rb').read()
    magic = data[:4]
    if magic not in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):
        raise SystemExit(f'{path}: not a little-endian Mach-O file')
    is64 = magic == b'\xcf\xfa\xed\xfe'
    ncmds = struct.unpack_from('<I', data, 16)[0]
    off, symtab = (32 if is64 else 28), None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, off)
        if cmd == 0x2:	# LC_SYMTAB
            symtab = struct.unpack_from('<IIII', data, off + 8)
        off += cmdsize
    if not symtab:
        raise SystemExit(f'{path}: no LC_SYMTAB (stripped kernel?)')
    symoff, nsyms, stroff, _ = symtab
    entsize = 16 if is64 else 12
    symbols = {}
    for i in range(nsyms):
        base = symoff + i * entsize
        n_strx, n_type = struct.unpack_from('<IB', data, base)
        n_value = struct.unpack_from('<Q' if is64 else '<I', data, base + 8)[0]
        if n_strx == 0 or (n_type & 0xe0) or (n_type & 0x0e) == 0:	# N_STAB / N_UNDF
            continue
        end = data.index(b'\0', stroff + n_strx)
        symbols[data[stroff + n_strx:end].decode('utf-8', 'replace')] = n_value
    return symbols


def find_qemu(name='qemu-system-aarch64'):
    """QEMU is not on PATH on a stock Windows install; probe the usual places."""
    env = os.environ.get('QEMU_SYSTEM_AARCH64')
    if env and os.path.exists(env):
        return env
    p = shutil.which(name)
    if p:
        return p
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    for d in (os.path.join(pf, 'QEMU'), os.path.join(pf, 'qemu'),
              r'C:\Program Files\QEMU', r'D:\QEMU', r'D:\qemu'):
        for exe in (name + '.exe', name):
            if os.path.exists(os.path.join(d, exe)):
                return os.path.join(d, exe)
    raise SystemExit(f'{name} not found: install QEMU or set QEMU_SYSTEM_AARCH64')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('payload'); ap.add_argument('outdir')
    ap.add_argument('--secs', type=float, default=20)
    ap.add_argument('--gdb', action='store_true', help='start halted, gdb stub on :1234, no timeout')
    ap.add_argument('--hmp', action='append', default=[], help='extra monitor command run after stop (repeatable), e.g. "x /8gx 0xfffffe..."')
    ap.add_argument('--cpu', default='cortex-a76')	# ARMv8.2 like Kryo 470, same ID_AA64MMFR0
    ap.add_argument('--kernel', help='matching unstripped kernel, for exact framebuffer text decoding (uses llvm-nm)')
    ap.add_argument('--disk', help='raw disk image appended as virtio-blk on the first virtio-mmio transport (0x0a000000, kernel 0082+)')
    ap.add_argument('--dump-symbol', action='append', default=[], metavar='NAME:SIZE',
                    help='at snapshot time pmemsave the kernel global NAME (SIZE bytes, 0x.. ok) to OUTDIR/NAME.bin; repeatable, requires --kernel')
    ap.add_argument('--fail-on-panic', action='store_true', help='exit 2 for a visible panic, 3 for incomplete/blank decoding; requires --kernel')
    ap.add_argument('--snap-every', type=float, default=0, metavar='SECS',
                    help='save fb-NNNs.png every SECS seconds of guest time (0 = only the final one)')
    a = ap.parse_args()
    if a.fail_on_panic and not a.kernel:
        ap.error('--fail-on-panic requires --kernel')
    if a.dump_symbol and not a.kernel:
        ap.error('--dump-symbol requires --kernel')
    payload = open(a.payload, 'rb').read()
    font = None
    symbols = {}
    if a.kernel:
        nm = shutil.which('llvm-nm')
        if nm:
            for line in subprocess.check_output([nm, '--defined-only', a.kernel], text=True).splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    symbols[parts[-1]] = int(parts[0], 16)
        else:
            symbols = macho_symbols(a.kernel)	# Windows: no llvm-nm installed
        fonts = [va for name, va in symbols.items() if name == '_iso_font']
        if len(fonts) != 1:
            ap.error('kernel must contain exactly one _iso_font symbol')
        font = payload_data(payload, fonts[0], 256 * 16)
    dumps = []
    for spec in a.dump_symbol:
        name, _, size = spec.rpartition(':')
        if not name or not size:
            ap.error(f'--dump-symbol expects NAME:SIZE, got {spec!r}')
        if name not in symbols:
            name = '_' + name
        if name not in symbols:
            ap.error(f'symbol {spec!r} not in the kernel')
        dumps.append((name.lstrip('_'), symbols[name], int(size, 0)))
    os.makedirs(a.outdir, exist_ok=True)
    out = os.path.abspath(a.outdir)
    q_out = out.replace('\\', '/')	# QEMU chardev paths: forward slashes on Windows

    phys_base, img, ba_pa, entry_pa, virt_base = build_image(payload)
    open(f'{out}/ram.bin', 'wb').write(img)
    open(f'{out}/stub.bin', 'wb').write(stub(ba_pa, entry_pa))
    print(f'phys_base={phys_base:#x} boot_args={ba_pa:#x} entry={entry_pa:#x}', file=sys.stderr)

    mon = f'{out}/mon.sock'
    if os.name == 'nt':
        # No usable AF_UNIX on Windows QEMU builds: pick a free TCP port.
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        mon_port = probe.getsockname()[1]
        probe.close()
        mon_arg = f'tcp:127.0.0.1:{mon_port},server,nowait'
        mon = f'{mon};{mon_port}'
    else:
        mon_arg = f'unix:{mon},server,nowait'
    if os.path.exists(mon):
        os.unlink(mon)
    # virt RAM starts at 0x40000000: cover the payload's RAM (mkadt.py --highmem needs 8G).
    mem_size = struct.unpack_from('<Q', payload, 0x40)[0]
    ram_gib = max(4, -(-(phys_base + mem_size - 0x40000000) // (1 << 30)))
    qemu = find_qemu()
    cmd = [qemu, '-M', 'virt,gic-version=3', '-cpu', a.cpu, '-smp', '1', '-m', f'{ram_gib}G',
           '-nographic', '-nodefaults', '-serial', f'file:{q_out}/serial.txt', '-qmp', mon_arg,
           '-d', 'int,guest_errors,unimp', '-D', f'{out}/qemu.log', '-no-reboot',
           '-device', f'loader,file={out}/ram.bin,addr={phys_base:#x},force-raw=on',
           '-device', f'loader,file={out}/stub.bin,addr={STUB_PA:#x},force-raw=on',
           '-device', f'loader,addr={STUB_PA:#x},cpu-num=0']
    if a.disk:
        cmd += ['-drive', f'file={a.disk},format=raw,if=none,id=hd0',
                '-device', 'virtio-blk-device,drive=hd0',
                '-global', 'virtio-mmio.force-legacy=false']
    if a.gdb:
        cmd += ['-S', '-gdb', 'tcp::1234']
    print('RUN:', qemu, file=sys.stderr)
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    if a.gdb:
        print('gdb stub on :1234; QMP socket:', mon, file=sys.stderr)
        p.wait()
        return
    t0 = time.time()
    next_snap = a.snap_every if a.snap_every > 0 else None
    while p.poll() is None and time.time() - t0 < a.secs:
        time.sleep(0.2)
        if next_snap is not None and time.time() - t0 >= next_snap:
            buf = f'{out}/fb-snap.raw'
            try:
                qmp(mon, 'stop', pmemsaves=[(FB_BASE, FB_STRIDE * FB_H, buf)])
                qmp(mon, 'cont')
                write_png(f'{out}/fb-{int(next_snap):03d}s.png', open(buf, 'rb').read())
                print(f'snapshot fb-{int(next_snap):03d}s.png', file=sys.stderr)
                os.unlink(buf)
            except OSError as e:
                print(f'snapshot failed: {e}', file=sys.stderr)
            next_snap += a.snap_every
    result = {'status': 'qemu_exited', 'qemu_returncode': p.returncode}
    if p.poll() is None:
        saves = [(FB_BASE, FB_STRIDE * FB_H, f'{out}/fb.raw')]
        saves += [(va + phys_base - virt_base, size, f'{out}/{name}.bin')
                  for name, va, size in dumps]
        _, regs, *extra = qmp(mon, 'stop', 'info registers', *a.hmp, pmemsaves=saves)
        open(f'{out}/regs.txt', 'w').write(regs + ''.join(f'\n(qemu) {c}\n{r}' for c, r in zip(a.hmp, extra)))
        p.kill()
        p.wait()
        raw = open(f'{out}/fb.raw', 'rb').read()
        write_png(f'{out}/fb.png', raw)
        result = {'status': 'not_decoded'}
        if font is not None:
            screen, unknown = decode_screen(raw, font)
            open(f'{out}/fb.txt', 'w').write(screen)
            result = screen_result(screen, unknown)
        os.unlink(f'{out}/fb.raw')
    else:
        print(f'qemu exited early: {p.returncode}', file=sys.stderr)
    for f in ('ram.bin',):
        os.unlink(f'{out}/{f}')
    open(f'{out}/result.json', 'w').write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), file=sys.stderr)
    print(f'{out}/fb.png  {out}/regs.txt  {out}/qemu.log', file=sys.stderr)
    if a.fail_on_panic and result['status'] == 'panic':
        return 2
    if result['status'] == 'qemu_exited':
        return 1


if __name__ == '__main__':
    sys.exit(main())
