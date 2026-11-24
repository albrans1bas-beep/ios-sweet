# Working on ios-sweet

iOS 16 (XNU 8796 + iPhone X 16.7.16 userland) on a Redmi Note 10 Pro (`sweet`,
SM7150). Repo = patched kernel series, bare-metal boot shim, HFS+ root builder,
and a QEMU test harness. Read `README.md` for architecture; read the top
"Agent handoff" section of `NOTES.md` before doing anything — it is the live
state (blocker, artifact paths, next action). Git log can be newer than
`NOTES.md`; check both.

## Environment (verified, do not re-derive)

- Windows/PowerShell host, **no WSL**. Rootfs builds (`mkiosroot.sh`,
  `sweet-root-tweaks.py` need `hfsprogs`/`apfs-fuse`) therefore happen in CI.
- All multi-GB downloads/artifacts live on D: — `D:\ios\{ipsw16,kernel,run,rootfs}`.
  Keep them out of the repo.
- QEMU 11.1 is at `C:\Program Files\QEMU`, **not on PATH** (tools probe that dir;
  `QEMU_SYSTEM_AARCH64` overrides).
- `gh run download` stalls on this host. Use `tools/sweet-image.py` for
  multi-GB artifacts, and the `kernel-latest` rolling release for kernels.
- No local symbolication: no HFS+ reader / `ipsw` on Windows. Use
  `.github/workflows/symbolicate.yml` (unsliced addresses + rootfs run ID).

## Kernel and payload

```sh
gh workflow run xnu.yml -R albrans1bas-beep/ios-sweet   # macos-15-intel only
python tools/xnu-cc.py ...        # single-file compile check from a CI log
python tools/mkadt.py shim/out/sweet.adt --highmem
python tools/mkpayload.py xnu/out/kernel.development.sweet shim/out/sweet.adt \
    shim/out/payload.bin --cmdline '<see README boot-arg table>'
shim/build.sh shim/out/payload.bin                      # -> shim/out/shim-boot.img
```

- Patches are applied in numeric order; a new patch takes the next free number
  and must apply cleanly on the current tree (regenerate against it, do not
  hand-patch context). Features are boot-arg gated.
- `macos-13`/`macos-14` runners are gone; toolchain skew with Xcode 16 is
  handled by in-tree patch 0091 (`IORPC.kernelContent`).

## QEMU loop

```sh
python tools/qemu-sweet.py D:/ios/run/payload.bin D:/ios/run/out \
  --secs 2700 --snap-every 300 --disk D:/ios/rootfs/rootfs.img --overlay \
  --kernel D:/ios/kernel/kernel.development.sweet \
  --display egl-headless --gpu --input both
```

- **Always `--overlay`**: SIGKILLing QEMU on a raw disk corrupts the HFS+ volume.
- Guest input only reaches virtio-input through a graphics device that owns a
  QemuConsole (`--display egl-headless --gpu`); `-nographic`/`--vnc` alone drop
  events. `virtual-machine monitor` sockets can wedge after a killed client —
  exit cleanly.
- TCG is slow: ~20 min guest time to the lock screen; do not send keys before
  `SWEET FB: display on` / the lock screen appears.
- Live view/input: `tools/sweet-view.py --outdir ... --display egl-headless`,
  `tools/sweet-input.py --mon ... tap X Y`.

## Device / destructive rules (user-approved)

- `fastboot boot` temporary images only; nothing is flashed to boot partitions.
- iOS root lives in the existing `userdata` partition (no GPT changes). Android
  userdata may be erased (approved 2026-10-15). Never touch bootloader,
  recovery, modem, `persist`/`fsg`/`modemst*`, or the backups.
- Confirm every destructive device step with the user before running it.
- Keep device dumps, serials, calibration data and Apple images out of git
  (`.gitignore` covers `backup/`, `dtb/`, `recovery/`, `research/`).

## Working style

- Commit completed changes; use `gh` for CI runs and logs.
- Update the `NOTES.md` handoff after each milestone and before stopping:
  commit SHAs, run IDs, exact build/boot results, and clearly separate
  observations from guesses.
- Untracked `tools/_*.py`, `--help`, `hdr-rootfs.txt` are local scratch — leave
  them alone unless asked.
