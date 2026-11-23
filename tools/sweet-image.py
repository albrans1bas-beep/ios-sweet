#!/usr/bin/env python3
"""Fetch the CI-built iOS root image (rootfs.yml `ios-rootfs` artifact).

  sweet-image.py [--run RUN_ID] [--repo OWNER/NAME] [--out D:\\ios\\rootfs\\rootfs.img]

Picks the newest successful rootfs run that still has an unexpired
`ios-rootfs` artifact, downloads it with `gh`, and unpacks the gzip stream
into OUT (streaming, so the 15 GB image never needs a second copy on disk).
Artifacts expire (3 days), so a rebuild is the fix when none is left.
"""
import argparse, glob, json, os, shutil, subprocess, sys, tempfile, zlib

CHUNK = 1 << 22


def gh(*args, **kw):
    return subprocess.run(['gh'] + list(args), capture_output=True, text=True, **kw)


def default_repo():
    r = gh('repo', 'view', '--json', 'nameWithOwner')
    if r.returncode:
        sys.exit('cannot determine the repo; pass --repo OWNER/NAME')
    return json.loads(r.stdout)['nameWithOwner']


def find_run(repo, want):
    """Newest successful rootfs run with a live ios-rootfs artifact."""
    r = gh('run', 'list', '--repo', repo, '--workflow', 'rootfs.yml', '--limit', '30',
           '--json', 'databaseId,status,conclusion,createdAt')
    if r.returncode:
        sys.exit(r.stderr.strip())
    for run in json.loads(r.stdout):
        if run['conclusion'] != 'success':
            continue
        if want and int(run['databaseId']) != want:
            continue
        a = gh('api', f'repos/{repo}/actions/runs/{run["databaseId"]}/artifacts')
        if a.returncode:
            continue
        arts = [x for x in json.loads(a.stdout).get('artifacts', [])
                if x['name'] == 'ios-rootfs' and not x['expired']]
        if arts:
            return run['databaseId'], arts[0]['size_in_bytes']
    return None, 0


def unpack(gz_path, out):
    """Stream-decompress gz_path into out (gzip member, possibly multi-part)."""
    total = 0
    d = zlib.decompressobj(zlib.MAX_WBITS | 16)
    with open(gz_path, 'rb') as fi, open(out, 'wb') as fo:
        while True:
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            data = d.decompress(chunk)
            fo.write(data)
            total += len(data)
        fo.write(d.flush())
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', type=int, help='rootfs run id (default: newest with an image)')
    ap.add_argument('--repo', help='OWNER/NAME (default: this repo)')
    ap.add_argument('--out', default=os.path.join('D:\\', 'ios', 'rootfs', 'rootfs.img'))
    ap.add_argument('--keep-gz', action='store_true')
    a = ap.parse_args()
    repo = a.repo or default_repo()
    run, size = find_run(repo, a.run)
    if not run:
        sys.exit('no unexpired ios-rootfs artifact: dispatch rootfs.yml with keep_image=true')
    print(f'run {run}: ios-rootfs {size / 1e9:.2f} GB', flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    dl = tempfile.mkdtemp(prefix='ios-rootfs-')
    try:
        r = gh('run', 'download', str(run), '--repo', repo, '-n', 'ios-rootfs', '-D', dl)
        if r.returncode:
            sys.exit(r.stderr.strip())
        files = glob.glob(os.path.join(dl, '**', '*.gz'), recursive=True) or \
            glob.glob(os.path.join(dl, '**', '*.img'), recursive=True)
        if not files:
            sys.exit(f'nothing to unpack in {dl}')
        src = files[0]
        if src.endswith('.gz'):
            print(f'unpacking {os.path.basename(src)} -> {a.out}', flush=True)
            n = unpack(src, a.out)
            if not a.keep_gz:
                os.unlink(src)
        else:
            print(f'copying {src} -> {a.out}', flush=True)
            shutil.copyfile(src, a.out)
            n = os.path.getsize(a.out)
        print(f'{a.out}: {n / 1e9:.2f} GB', flush=True)
    finally:
        shutil.rmtree(dl, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
