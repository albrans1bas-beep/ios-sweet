import socket, json, sys, re, datetime
ep, out = sys.argv[1], sys.argv[2]
host, port = ep.rsplit(':', 1)
try:
    s = socket.create_connection((host, int(port)), timeout=15)
    f = s.makefile('rw'); json.loads(f.readline())
    def cmd(c):
        f.write(json.dumps(c) + '\n'); f.flush()
        while True:
            r = json.loads(f.readline())
            if 'return' in r or 'error' in r: return r
    cmd({'execute': 'qmp_capabilities'})
    cmd({'execute': 'pmemsave', 'arguments': {'val': 0xb1c525f8, 'size': 0x200000,
        'filename': out + '/ring-mon.bin'}})
    s.close()
    d = open(out + '/ring-mon.bin', 'rb').read().rstrip(b'\0').decode('utf-8', 'replace')
    now = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'{now} execSB={d.count(chr(10)+"exec SpringBoard")} '
          f'crashSB={d.count("CRASH SpringBoard")} displayOn={d.count("display on")} '
          f'hid={d.count("SWEET HID: touch")}')
except Exception as e:
    print(f"{datetime.datetime.now().strftime('%H:%M:%S')} PROBE-FAIL {type(e).__name__}: {e}")
