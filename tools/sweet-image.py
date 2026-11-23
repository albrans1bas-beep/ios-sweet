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
import argparse, json, os, subprocess, sys, time, urllib.error, urllib.request, zipfile, zlib

CHUNK = 1 << 22
REPORT = 512 << 20	# progress line every 512 MB
STALL_SECS = 60		# a read blocking this long means the transfer died


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


def signed_url(repo, artifact_id, tok):
    """The Azure SAS URL behind the API's 302 (no Authorization header)."""
    url = f'https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {tok}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'sweet-image',
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.geturl()	# 200 without a redirect: this URL is the blob itself
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            sys.exit(f'artifact download failed: HTTP {e.code} {e.reason}')
        return e.headers['Location']


def fetch_resumable(repo, art, tok, dest, tries=40):
    """Download the artifact zip, resuming after a stall instead of restarting.

    Multi-GB transfers to this host die silently (the socket stays open, no
    bytes arrive), which is why `gh run download` was abandoned. So: a hard
    socket timeout, a Range request from whatever is already on disk, and a
    fresh signed URL each retry (the SAS token expires).
    """
    total = art['size_in_bytes']
    done = os.path.getsize(dest) if os.path.exists(dest) else 0
    if done > total:		# a previous attempt was a different artifact
        os.unlink(dest)
        done = 0
    stalls = 0
    shown = done
    while done < total:
        try:
            url = signed_url(repo, art['id'], tok)
            req = urllib.request.Request(url, headers={'User-Agent': 'sweet-image'})
            if done:
                req.add_header('Range', f'bytes={done}-')
            with urllib.request.urlopen(req, timeout=STALL_SECS) as src:
                if done and src.status != 206:
                    # Range ignored: start over rather than append garbage.
                    print('  server ignored Range, restarting', flush=True)
                    done = 0
                    mode = 'wb'
                else:
                    mode = 'ab' if done else 'wb'
                with open(dest, mode) as fo:
                    while done < total:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        fo.write(chunk)
                        done += len(chunk)
                        if done - shown >= REPORT:
                            shown = done
                            print(f'  {done / 1e9:.2f} GB ({100 * done / total:.0f}%)', flush=True)
        except Exception as e:	# noqa: BLE001 - any network error is retryable
            stalls += 1
            if stalls >= tries:
                sys.exit(f'download gave up after {stalls} stalls at {done / 1e9:.2f} GB: {e}')
            print(f'  stalled at {done / 1e9:.2f} GB ({e.__class__.__name__}); retry {stalls}/{tries}', flush=True)
            time.sleep(3)
            continue
        if done < total:
            stalls += 1
            if stalls >= tries:
                sys.exit(f'download gave up at {done / 1e9:.2f} GB (server closed early)')
            print(f'  short read at {done / 1e9:.2f} GB; retry {stalls}/{tries}', flush=True)
            time.sleep(3)
    return done


def open_artifact(repo, artifact_id, tok):
    """Return a readable stream for the artifact zip (follows the 302 by hand)."""
    return urllib.request.urlopen(urllib.request.Request(
        signed_url(repo, artifact_id, tok), headers={'User-Agent': 'sweet-image'}))


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
    n = fetch_resumable(repo, art, token(), zip_path)
    print(f'  {n / 1e9:.2f} GB downloaded', flush=True)
    total = unpack_zip(zip_path, a.out)
    if not a.keep_zip:
        os.unlink(zip_path)
    print(f'{a.out}: {total / 1e9:.2f} GB', flush=True)


if __name__ == '__main__':
    sys.exit(main())
