#!/usr/bin/env python3
"""Watch a running SWEET QEMU guest: live framebuffer window + PNG on demand.

  sweet-view.py [--mon tcp:127.0.0.1:PORT | --outdir DIR] [--fps N] [--scale N]

The kernel draws into a fixed physical framebuffer (FB_BASE, 1080x2400 BGRA),
so a live view is a QMP pmemsave loop, not a QEMU display device. qemu-sweet.py
writes the monitor endpoint to OUTDIR/mon.txt; --mon overrides it.

Keys in the window:  s  save a PNG next to OUTDIR     q / Esc  quit
                     h  Home button    p  power     +/-  volume
Mouse: click and drag on the picture is a touch in the guest (the guest polls
QEMU's virtio-tablet/keyboard, patch 0082, and turns it into HID events).
Without tkinter it still works headless: frames are written as PNG on change.

Everything here is read-only against the guest: stop/pmemsave/cont, so the
frame is torn-free and the guest loses at most one scheduling slice.
"""
import argparse, hashlib, importlib.util, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_qemu_sweet():
    spec = importlib.util.spec_from_file_location('qemu_sweet', os.path.join(HERE, 'qemu-sweet.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_endpoint(text):
    """Accept 'tcp:127.0.0.1:PORT[,server,nowait]' or a unix socket path."""
    text = text.strip()
    m = re.match(r'tcp:([^:,]+):(\d+)', text)
    if m:
        return f'{m.group(1)};{m.group(2)}'	# qemu-sweet's Windows convention
    return text


def read_endpoint(args):
    if args.mon:
        return parse_endpoint(args.mon)
    path = os.path.join(args.outdir or '.', 'mon.txt')
    if not os.path.exists(path):
        sys.exit(f'no monitor endpoint: {path} missing (pass --mon tcp:127.0.0.1:PORT)')
    return parse_endpoint(open(path).read())


def grab(qs, ep, raw_path):
    """One consistent frame: stop, pmemsave, cont."""
    qs.qmp(ep, 'stop')
    try:
        qs.qmp(ep, pmemsaves=[(qs.FB_BASE, qs.FB_STRIDE * qs.FB_H, raw_path)])
    finally:
        qs.qmp(ep, 'cont')
    return open(raw_path, 'rb').read()


AXIS_MAX = 0x7fff	# QEMU tablet axis range; the guest scales it to the panel


class Input:
    """Forward window clicks/keys to the guest through QMP.

    The guest has no USB/PS2 controller: qemu-sweet.py attaches
    virtio-tablet-device + virtio-keyboard-device, the kernel polls them
    (patch 0082) and feeds backboardd's HID services."""

    def __init__(self, qs, ep):
        self.qs, self.ep, self.sess, self.down = qs, ep, None, False

    def call(self, execute, **args):
        try:
            if self.sess is None:
                self.sess = self.qs.qmp_session(self.ep)
            r = self.sess[1](execute, **args)
        except Exception as e:
            print(f'input failed: {type(e).__name__}: {e}', flush=True)
            self.sess = None
            return None
        if 'error' in r:
            print(f'input {execute}: {r["error"]}', flush=True)
            self.sess = None
        return r

    def touch(self, x, y, down=None):
        if down is not None:
            self.down = down
        events = [
            {'type': 'abs', 'data': {'axis': 'x',
                                     'value': int(x * AXIS_MAX / (self.qs.FB_W - 1))}},
            {'type': 'abs', 'data': {'axis': 'y',
                                     'value': int(y * AXIS_MAX / (self.qs.FB_H - 1))}},
        ]
        if down is not None:
            events.append({'type': 'btn', 'data': {'down': down, 'button': 'left'}})
        # device=: QMP needs the graphics device that owns a QemuConsole
        # (qemu-sweet.py --gpu); naming the tablet is rejected.
        self.call('input-send-event', events=events, device='sweetgpu')

    def key(self, qcode):
        self.call('send-key', keys=[{'type': 'qcode', 'data': qcode}],
                  **{'hold-time': 100})

    def close(self):
        if self.sess:
            try:
                self.sess[0].close()
            except OSError:
                pass
            self.sess = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mon', help='QMP endpoint (default: OUTDIR/mon.txt)')
    ap.add_argument('--outdir', default=r'D:\ios\run\out', help='where mon.txt/PNGs live')
    ap.add_argument('--fps', type=float, default=2.0)
    ap.add_argument('--scale', type=int, default=0, help='integer subsample (0 = fit the screen)')
    a = ap.parse_args()
    qs = load_qemu_sweet()
    ep = read_endpoint(a)
    os.makedirs(a.outdir, exist_ok=True)
    raw_path = os.path.join(a.outdir, 'view.raw')
    png_path = os.path.join(a.outdir, 'view.png')

    try:
        import tkinter as tk
    except ImportError:
        tk = None
    if tk is None:
        sys.exit('tkinter missing: use --fps with qemu-sweet.py --snap-every instead')

    root = tk.Tk()
    root.title('Sweet Runner - iOS 16')
    screen_h = root.winfo_screenheight()
    scale = a.scale or max(1, -(-qs.FB_H // (screen_h - 120)))	# ceil
    label = tk.Label(root, text='connecting...', bg='black', fg='white',
                     width=qs.FB_W // scale // 8, height=qs.FB_H // scale // 16)
    label.pack()
    state = {'photo': None, 'last': None, 'frames': 0, 'saves': 0, 'err': ''}
    inp = Input(qs, ep)

    def panel_xy(e):
        return (min(max(e.x * scale, 0), qs.FB_W - 1),
                min(max(e.y * scale, 0), qs.FB_H - 1))

    def press(e):
        x, y = panel_xy(e)
        print(f'touch down {x},{y}', flush=True)
        inp.touch(x, y, True)

    def motion(e):
        if inp.down:
            x, y = panel_xy(e)
            inp.touch(x, y)

    def release(e):
        x, y = panel_xy(e)
        print(f'touch up {x},{y}', flush=True)
        inp.touch(x, y, False)

    label.bind('<ButtonPress-1>', press)
    label.bind('<B1-Motion>', motion)
    label.bind('<ButtonRelease-1>', release)

    def save_png(raw):
        state['saves'] += 1
        path = os.path.join(a.outdir, f'view-{state["saves"]:03d}.png')
        qs.write_png(path, raw)
        print(f'saved {path}', flush=True)

    def tick():
        try:
            raw = grab(qs, ep, raw_path)
            digest = hashlib.sha1(raw[::4096]).hexdigest()[:12]
            if digest != state['last']:
                qs.write_png(png_path, raw)
                photo = tk.PhotoImage(file=png_path)
                if scale > 1:
                    photo = photo.subsample(scale)
                label.configure(image=photo, width=0, height=0, text='')
                state['photo'] = photo	# keep a reference or Tk frees it
                state['last'] = digest
                state['frames'] += 1
                root.title(f'Sweet Runner - iOS 16  [{digest} frame {state["frames"]}]')
                print(f'frame {state["frames"]} {digest}', flush=True)
            state['err'] = ''
        except Exception as e:	# keep the window alive across QMP hiccups
            msg = f'{type(e).__name__}: {e}'
            if msg != state['err']:
                state['err'] = msg
                print(f'grab failed: {msg}', flush=True)
        root.after(max(50, int(1000 / a.fps)), tick)

    root.bind('<KeyPress-q>', lambda e: root.destroy())
    root.bind('<KeyPress-s>', lambda e: save_png(open(raw_path, 'rb').read()))
    root.bind('<Escape>', lambda e: root.destroy())
    root.bind('<KeyPress-h>', lambda e: inp.key('home'))
    root.bind('<KeyPress-p>', lambda e: inp.key('power'))
    root.bind('<KeyPress-plus>', lambda e: inp.key('volumeup'))
    root.bind('<KeyPress-equal>', lambda e: inp.key('volumeup'))
    root.bind('<KeyPress-minus>', lambda e: inp.key('volumedown'))
    root.after(1, tick)
    print(f'watching {ep} at {a.fps} fps, subsample {scale} ({qs.FB_W // scale}x{qs.FB_H // scale})',
          flush=True)
    root.mainloop()
    inp.close()
    try:
        os.unlink(raw_path)
    except OSError:
        pass


if __name__ == '__main__':
    sys.exit(main())
