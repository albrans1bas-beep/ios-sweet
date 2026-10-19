# Pivot: iOS dev environment on Windows ("Sweet Runner")

Goal: a Windows package that runs real iOS 16 (XNU + userland, already boots
to the home screen on sweet hardware) inside QEMU, plus a Windows-side build
toolchain and a `simctl`-like bridge, so developers can build and run iOS apps
without a Mac.

Non-goals (permanent): Face ID/SEP, real Apple activation against Apple
servers, App Store purchases from Apple's servers — trust roots are not
emulatable. Substitute: a local "Sweet App Store" catalog of user-built and
open-source iOS apps.

## Phases

- **P0 Infra** (2026-10-19): fork `super-xl/ios-sweet`, Actions enabled,
  baseline kernel build run 39274838234 dispatched. Local Windows loop set up:
  git, gh 2.102, Python 3.11, QEMU 11.1 (`C:\Program Files\QEMU`).
- **P1 QEMU kernel devices**: boot the existing userland under QEMU virt.
  - 0082 virtio-mmio transport (scan ADT `/arm-io/virtioN` nodes)
  - 0083 virtio-blk + root `rd=vroot0` (mirrors 0028 ufs root device)
  - 0084 ramfb-as-AppleCLCD (keep the copy-to-panel-present design; configure
    the framebuffer via fw_cfg DMA writes, kernel draws console + swaps)
  - 0085 virtio-input -> `sweet_hid_key()`/`sweet_hid_touch()` hooks
  - 0086 virtio-net -> `ue0` (reuse the 0075/0076 CDC-ECM ifnet design)
  - `tools/mkadt.py`: emit virtio nodes in the `--qemu` tree
  - `tools/qemu-sweet.py`: `-device ramfb`, virtio-blk root image, virtio
    input, virtio-net; QMP unix->TCP fallback for Windows
- **P2 Root image for QEMU**: `tools/mkiosroot-qemu.sh` variant of
  `mkiosroot.sh` (no UFS partitioning, HFS+ image file); guest
  `sweetbridge` launchd daemon (Mach-port server) for M3+.
- **P3 Windows Runner (GUI-first)**: installer + bezel window (Play),
  QEMU lifecycle, user-provided IPSW -> image build step (on the user's
  machine; Apple firmware is never shipped in the repo or packages).
- **P4 `sweetctl` + toolchain**: install/launch/console/screenshot/crash via
  virtio-console bridge; Swift-for-Windows + SDK headers extracted from the
  user's IPSW; UIKit "Hello World" template compiled on Windows.
- **P5 VS Code extension + docs + release v1.0.0.**
- **P6 (later) Sweet App Store** catalog UI inside the emulator.

## Working agreements for this fork

- Kernel patch series continues on top of upstream main (0081); patches are
  boot-arg gated and syntax-checked with `tools/xnu-cc.py` against the latest
  CI log before pushing.
- NOTES.md handoff is updated after each milestone (per repo AGENTS.md).
- No Apple firmware, device dumps, or serials go into git.
