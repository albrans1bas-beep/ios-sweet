#!/usr/bin/env python3
"""Send a touch or a key to a running SWEET guest over QMP.

  sweet-input.py [--mon tcp:127.0.0.1:PORT] tap X Y
  sweet-input.py [--mon ...] swipe X1 Y1 X2 Y2 [--steps N] [--ms M]
  sweet-input.py [--mon ...] key home|power|volumeup|volumedown

Coordinates are panel pixels (1080x2400). The guest side is patch 0082:
qemu-sweet.py attaches virtio-tablet-device/virtio-keyboard-device and the
kernel polls them, so QEMU's input layer reaches backboardd's HID services.
Without --mon the endpoint is read from OUTDIR/mon.txt (default D:\\ios\\run\\out).
"""
import argparse, importlib.util, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
AXIS_MAX = 0x7fff


def load_qemu_sweet():
    spec = importlib.util.spec_from_file_location('qemu_sweet', os.path.join(HERE, 'qemu-sweet.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_endpoint(text):
    m = re.match(r'tcp:([^:,]+):(\d+)', text.strip())
    if m:
        return f'{m.group(1)};{m.group(2)}'
    return text.strip()


class Touch:
    def __init__(self, qs, ep, device=None):
        self.qs, self.sess = qs, qs.qmp_session(ep)
        self.device = device

    def abs(self, x, y):
        return [
            {'type': 'abs', 'data': {'axis': 'x', 'value': int(x * AXIS_MAX / 1079)}},
            {'type': 'abs', 'data': {'axis': 'y', 'value': int(y * AXIS_MAX / 2399)}},
        ]

    def send(self, events):
        args = {'events': events}
        if self.device:
            args['device'] = self.device
        r = self.sess[1]('input-send-event', **args)
        if 'error' in r:
            sys.exit(f'input-send-event: {r["error"]}')
        return r

    def tap(self, x, y):
        self.send(self.abs(x, y) + [{'type': 'btn', 'data': {'down': True, 'button': 'left'}}])
        time.sleep(0.05)
        self.send([{'type': 'btn', 'data': {'down': False, 'button': 'left'}}])

    def swipe(self, x1, y1, x2, y2, steps, ms):
        self.send(self.abs(x1, y1) + [{'type': 'btn', 'data': {'down': True, 'button': 'left'}}])
        for i in range(1, steps + 1):
            time.sleep(ms / 1000.0)
            self.send(self.abs(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps))
        time.sleep(0.05)
        self.send([{'type': 'btn', 'data': {'down': False, 'button': 'left'}}])

    def key(self, qcode):
        r = self.sess[1]('send-key', keys=[{'type': 'qcode', 'data': qcode}],
                         **{'hold-time': 100})
        if 'error' in r:
            sys.exit(f'send-key {qcode}: {r["error"]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mon', help='QMP endpoint (default: OUTDIR/mon.txt)')
    ap.add_argument('--device', default='sweetgpu',
                    help='QMP device id to inject into: the graphics device that owns a '
                         'QemuConsole (qemu-sweet.py --gpu, default sweetgpu). Naming the '
                         'virtio-tablet fails with "not bound to a QemuConsole".')
    ap.add_argument('--outdir', default=r'D:\ios\run\out')
    ap.add_argument('--steps', type=int, default=12)
    ap.add_argument('--ms', type=int, default=16, help='delay between swipe points')
    sub = ap.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('tap')
    t.add_argument('x', type=int)
    t.add_argument('y', type=int)
    s = sub.add_parser('swipe')
    for n in ('x1', 'y1', 'x2', 'y2'):
        s.add_argument(n, type=int)
    k = sub.add_parser('key')
    k.add_argument('qcode')
    a = ap.parse_args()

    ep = parse_endpoint(a.mon) if a.mon else parse_endpoint(
        open(os.path.join(a.outdir, 'mon.txt')).read())
    qs = load_qemu_sweet()
    touch = Touch(qs, ep, a.device or None)
    if a.cmd == 'tap':
        print(f'tap {a.x},{a.y}')
        touch.tap(a.x, a.y)
    elif a.cmd == 'swipe':
        print(f'swipe {a.x1},{a.y1} -> {a.x2},{a.y2} in {a.steps} steps')
        touch.swipe(a.x1, a.y1, a.x2, a.y2, a.steps, a.ms)
    else:
        print(f'key {a.qcode}')
        touch.key(a.qcode)


if __name__ == '__main__':
    sys.exit(main())
