#!/usr/bin/env python3
"""Extract OS.dmg/SystemOS.dmg/AppOS.dmg from a restore IPSW (same logic as CI)."""
import os, plistlib, sys, zipfile

ipsw = sys.argv[1]
outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)
z = zipfile.ZipFile(ipsw)
bm = plistlib.loads(z.read('BuildManifest.plist'))
m = bm['BuildIdentities'][0]['Manifest']
dmg = {k: v['Info']['Path'] for k, v in m.items()
       if v.get('Info', {}).get('Path', '').endswith('.dmg')}
print('dmg keys:', {k: os.path.basename(v) for k, v in dmg.items()})
want = {}
for k, p in dmg.items():
    lk = k.lower()
    if lk == 'os' or 'operating' in lk:
        want['OS'] = p
    elif 'system' in lk:
        want['SystemOS'] = p
    elif 'app' in lk:
        want['AppOS'] = p
assert set(want) == {'OS', 'SystemOS', 'AppOS'}, want
for k, path in want.items():
    print('extracting', k, '->', path, flush=True)
    z.extract(path, os.path.join(outdir, '_x'))
    dst = os.path.join(outdir, f'{k}.dmg')
    os.replace(os.path.join(outdir, '_x', path), dst)
    print(k, 'ok', os.path.getsize(dst), flush=True)
try:
    os.rmdir(os.path.join(outdir, '_x'))
except OSError:
    pass
