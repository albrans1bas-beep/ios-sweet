#!/usr/bin/env python3
"""Fetch the CI-built iOS root image (rootfs.yml `ios-rootfs` artifact).

  sweet-image.py [--run RUN_ID] [--repo OWNER/NAME] [--out D:\\ios\\rootfs\\rootfs.img]

Picks the newest successful rootfs run that still has an unexpired
`ios-rootfs` artifact, downloads the artifact zip from the API and unpacks
it in one streaming pass: the 15 GB image is never duplicated on disk.

`gh run download` is deliberately not used - it stalled at 0 B/s on this
host, and the API redirect is easy to follow with progress reporting.
Artifacts expire (3 days), so a rebuild is the fix when none is left.
"""
import argparse, json, os, subprocess, sys, urllib.error, urllib.request, zipfile, zlib

CHUNK = 1 << 22
REPORT = 512 << 20	# progress line every 512 MB


def gh(*args):
    return subprocess.run(['gh'] + list(args), capture_output=True, text=True)


def default_repo():
    r = gh('repo', 'view', '--json', 'nameWithOwner')
    if r.returncode:
        sys.exit('cannot determine the repo; pass --repo OWNER/NAME')
    return json.loads(r.stdout)['nameWithOwner']


def token():
    r = gh('auth', 'token')
    if r.returncode:
        sys.exit('gh is not authenticated')
    return r.stdout.strip()


def find_artifact(repo, want):
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
        for art in json.loads(a.stdout).get('artifacts', []):
            if art['name'] == 'ios-rootfs' and not art['expired']:
                return int(run['databaseId']), art
    return None, None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None	# surface the 302 so the signed blob URL can be used bare


def open_artifact(repo, artifact_id, tok):
    """Return a readable stream for the artifact zip (follows the 302 by hand)."""
    url = f'https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {tok}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'sweet-image',
    })
    try:
        return urllib.request.build_opener(NoRedirect).open(req)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            sys.exit(f'artifact download failed: HTTP {e.code} {e.reason}')
        loc = e.headers['Location']	# Azure blob SAS URL: no Authorization header
        return urllib.request.urlopen(urllib.request.Request(loc, headers={'User-Agent': 'sweet-image'}))


def stream_to_file(src, dest, total=0):
    done = 0
    with open(dest, 'wb') as fo:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            fo.write(chunk)
            done += len(chunk)
            if done % REPORT < CHUNK:
                pct = f' ({100 * done / total:.0f}%)' if total else ''
                print(f'  {done / 1e9:.2f} GB{pct}', flush=True)
    return done


def unpack_zip(zip_path, out):
    """Stream the single image member out of the artifact zip (gunzip on the fly)."""
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if n.endswith(('.gz', '.img'))]
        if not members:
            sys.exit(f'no .gz/.img member in {zip_path}: {z.namelist()}')
        name = members[0]
        gz = name.endswith('.gz')
        print(f'unpacking {name} -> {out}' + (' (gunzip)' if gz else ''), flush=True)
        total = 0
        with z.open(name) as fi, open(out, 'wb') as fo:
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
            while True:
                chunk = fi.read(CHUNK)
                if not chunk:
                    break
                data = dec.decompress(chunk) if dec else chunk
                fo.write(data)
                total += len(data)
                if total % REPORT < CHUNK:
                    print(f'  {total / 1e9:.2f} GB', flush=True)
            if dec:
                fo.write(dec.flush())
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', type=int, help='rootfs run id (default: newest with an image)')
    ap.add_argument('--repo', help='OWNER/NAME (default: this repo)')
    ap.add_argument('--out', default=os.path.join('D:\\', 'ios', 'rootfs', 'rootfs.img'))
    ap.add_argument('--tmp', help='download dir (default: <out dir>/dl; never the system temp)')
    ap.add_argument('--keep-zip', action='store_true')
    a = ap.parse_args()
    repo = a.repo or default_repo()
    run, art = find_artifact(repo, a.run)
    if not art:
        sys.exit('no unexpired ios-rootfs artifact: dispatch rootfs.yml with keep_image=true')
    print(f'run {run}: {art["name"]} {art["size_in_bytes"] / 1e9:.2f} GB', flush=True)
    outdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(outdir, exist_ok=True)
    dl = a.tmp or os.path.join(outdir, 'dl')
    os.makedirs(dl, exist_ok=True)
    zip_path = os.path.join(dl, f'ios-rootfs-{run}.zip')
    print(f'downloading -> {zip_path}', flush=True)
    n = stream_to_file(open_artifact(repo, art['id'], token()), zip_path, art['size_in_bytes'])
    print(f'  {n / 1e9:.2f} GB downloaded', flush=True)
    total = unpack_zip(zip_path, a.out)
    if not a.keep_zip:
        os.unlink(zip_path)
    print(f'{a.out}: {total / 1e9:.2f} GB', flush=True)


if __name__ == '__main__':
    sys.exit(main())
