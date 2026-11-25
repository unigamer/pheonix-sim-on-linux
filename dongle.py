#!/usr/bin/env python3
"""dongle.py – software PhoenixRC USB dongle.

Phoenix RC's Simulator.exe reads its dongle (USB 1781:0898) with raw
ReadFile(handle, buf, 9): report-id byte + 8 bytes, one unsigned byte per
channel.  Normal controllers have different report sizes, so Wine rejects
those reads and the sticks never move.

This script reads any Linux joystick (evdev: Xbox pad, BetaFPV over
Bluetooth/USB, EdgeTX radio, ...) and re-publishes it through /dev/uhid as a
virtual 1781:0898 HID device that sends 8-byte reports in the same layout.
Wine passes that device to the game through hidraw.

Needs write access to /dev/uhid and the resulting /dev/hidraw* node, which
udev/70-pheonix.rules provides (install once with sudo).

Usage:
  dongle.py [--source /dev/input/eventN | --source-name substring]
            [--map X,Y,RX,RY,Z,RZ,THROTTLE,RUDDER] [--list] [--verbose]
"""
import argparse
import fcntl
import glob
import os
import select
import struct
import sys
import time
from types import SimpleNamespace

# --- minimal evdev (no python-evdev needed: SteamOS has no pip) ------------
EV_KEY, EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_THROTTLE, ABS_RUDDER, ABS_WHEEL, ABS_GAS, ABS_BRAKE = 0x06, 0x07, 0x08, 0x09, 0x0A
ABS_HAT0X, ABS_HAT0Y, ABS_MISC = 0x10, 0x11, 0x28
BTN_TOOL_PEN, BTN_TOUCH = 0x140, 0x14A
BUS_VIRTUAL = 0x06  # not BUS_USB: the kernel's hid-pxrc driver would grab a USB 1781:0898 and rewrite bytes 1 and 7
INPUT_EVENT = struct.Struct("llHHi")  # struct input_event (timeval + type, code, value)


def _ior(nr, size):
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


def _bits(fd, ev, nbytes):
    buf = bytearray(nbytes)
    fcntl.ioctl(fd, _ior(0x20 + ev, nbytes), buf)
    return {i for i in range(nbytes * 8) if buf[i // 8] >> (i % 8) & 1}


class InputDevice:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            name = bytearray(256)
            fcntl.ioctl(self.fd, _ior(0x06, len(name)), name)
            self.name = name.split(b"\0", 1)[0].decode(errors="replace")
            ident = bytearray(8)
            fcntl.ioctl(self.fd, _ior(0x02, 8), ident)
            bus, vendor, product, version = struct.unpack("HHHH", ident)
            self.info = SimpleNamespace(bustype=bus, vendor=vendor, product=product)
            self.abs_codes = _bits(self.fd, EV_ABS, 8) if EV_ABS in _bits(self.fd, 0, 4) else set()
            self.keys = _bits(self.fd, EV_KEY, 96)
        except OSError:
            os.close(self.fd)
            raise

    def absinfo(self):
        out = {}
        for code in self.abs_codes:
            buf = bytearray(24)
            fcntl.ioctl(self.fd, _ior(0x40 + code, 24), buf)
            v, lo, hi, _fuzz, _flat, _res = struct.unpack("6i", buf)
            out[code] = SimpleNamespace(value=v, min=lo, max=hi)
        return out

    def read(self):
        data = os.read(self.fd, INPUT_EVENT.size * 64)
        if not data:
            raise OSError("device gone")
        for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
            _s, _us, typ, code, value = INPUT_EVENT.unpack_from(data, off)
            yield SimpleNamespace(type=typ, code=code, value=value)

    def close(self):
        os.close(self.fd)


def list_devices():
    return sorted(glob.glob("/dev/input/event*"), key=lambda p: int(p.rsplit("t", 1)[1]))

VID, PID = 0x1781, 0x0898

# Vendor-defined top-level collection: hid-generic creates no evdev device
# for it (so SDL ignores it) and Wine picks it up through hidraw.
# 8-byte input report and 8-byte output report, no report IDs.
REPORT_DESCRIPTOR = bytes([
    0x06, 0x00, 0xFF,        # Usage Page (Vendor 0xFF00)
    0x09, 0x01,              # Usage (1)
    0xA1, 0x01,              # Collection (Application)
    0x15, 0x00,              #   Logical Minimum (0)
    0x26, 0xFF, 0x00,        #   Logical Maximum (255)
    0x75, 0x08,              #   Report Size (8)
    0x95, 0x08,              #   Report Count (8)
    0x09, 0x01,              #   Usage (1)
    0x81, 0x02,              #   Input (Data,Var,Abs)
    0x09, 0x02,              #   Usage (2)
    0x91, 0x02,              #   Output (Data,Var,Abs)
    0xC0,                    # End Collection
])

UHID_DESTROY, UHID_OUTPUT, UHID_CREATE2, UHID_INPUT2 = 1, 6, 11, 12
UHID_EVENT_SIZE = 4 + 4372  # type + largest union member (create2_req)

AXIS_NAMES = {
    "X": ABS_X, "Y": ABS_Y, "Z": ABS_Z, "RX": ABS_RX, "RY": ABS_RY,
    "RZ": ABS_RZ, "THROTTLE": ABS_THROTTLE, "RUDDER": ABS_RUDDER,
    "WHEEL": ABS_WHEEL, "GAS": ABS_GAS, "BRAKE": ABS_BRAKE,
    "HAT0X": ABS_HAT0X, "HAT0Y": ABS_HAT0Y, "MISC": ABS_MISC,
    "NONE": None,
}
# Report layout seen by Phoenix: byte0=ch1, byte1=unused, byte2..6=ch2..6,
# byte7=ch7/8 (multiplexed, not fully understood).
DEFAULT_MAP = "X,NONE,Y,RX,RY,Z,RZ,THROTTLE"


def uhid_create(fd, name):
    ev = struct.pack("<I128s64s64sHHIIII4096s", UHID_CREATE2,
                     name.encode()[:127], b"pheonix-dongle", b"",
                     len(REPORT_DESCRIPTOR), BUS_VIRTUAL, VID, PID, 0x0100, 0,
                     REPORT_DESCRIPTOR)
    os.write(fd, ev.ljust(UHID_EVENT_SIZE, b"\0"))


def uhid_input(fd, data):
    ev = struct.pack("<IH4096s", UHID_INPUT2, len(data), data)
    os.write(fd, ev.ljust(UHID_EVENT_SIZE, b"\0"))


def is_joystick(dev):
    return ABS_X in dev.abs_codes and ABS_Y in dev.abs_codes


# Gamepads (incl. the Steam Deck's own controls and Steam Input's virtual
# Xbox pads) are only used when no other joystick, e.g. a radio, is present.
GAMEPAD_WORDS = ("x-box", "xbox", "steam", "gamepad", "controller", "pad")


def find_sources():
    out = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except OSError:
            continue
        keep = (not d.name.startswith("pheonix-dongle") and is_joystick(d)
                # skip touchpads / tablets / accelerometers
                and BTN_TOUCH not in d.keys and BTN_TOOL_PEN not in d.keys
                and "Accelerometer" not in d.name and "Motion Sensors" not in d.name)
        if keep:
            out.append(d)
        else:
            d.close()
    return out


def pick_source(args):
    if args.source:
        return InputDevice(args.source)
    srcs = find_sources()
    if args.source_name:
        srcs = [d for d in srcs if args.source_name.lower() in d.name.lower()]
    srcs.sort(key=lambda d: (any(w in d.name.lower() for w in GAMEPAD_WORDS),
                             int(d.path.rsplit("t", 1)[1])))
    for d in srcs[1:]:
        d.close()
    return srcs[0] if srcs else None


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", help="evdev path, e.g. /dev/input/event21")
    p.add_argument("--source-name", help="pick first joystick whose name contains this")
    p.add_argument("--map", default=DEFAULT_MAP,
                   help=f"8 comma-separated axes for bytes 0..7 (default {DEFAULT_MAP})")
    p.add_argument("--list", action="store_true", help="list candidate joysticks and exit")
    p.add_argument("--wait", action="store_true", help="wait until a joystick appears")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--raw-file", help="testing: send the 8 hex bytes found in this file "
                   "instead of joystick data (re-read every 50 ms)")
    a = p.parse_args()

    if a.list:
        for d in find_sources():
            i = d.info
            print(f"{d.path}  {i.vendor:04x}:{i.product:04x}  {d.name}")
            d.close()
        return

    names = [n.strip().upper() for n in a.map.split(",")]
    if len(names) != 8 or any(n not in AXIS_NAMES for n in names):
        sys.exit(f"--map needs 8 of: {', '.join(AXIS_NAMES)}")
    mapping = [AXIS_NAMES[n] for n in names]

    src = pick_source(a)
    if src is None and not a.wait:
        sys.exit("No joystick found (use --list, --wait or --source).")

    try:
        fd = os.open("/dev/uhid", os.O_RDWR | os.O_CLOEXEC)
    except PermissionError:
        sys.exit("Cannot open /dev/uhid – run: sudo ./setup-host.sh")

    # The dongle stays plugged in for the whole session: Phoenix only scans
    # for it at startup.  The source joystick may come and go (Bluetooth).
    uhid_create(fd, "pheonix-dongle")
    print("Virtual PhoenixRC dongle 1781:0898 created", flush=True)

    absinfo, state = {}, {}

    def attach(dev):
        absinfo.clear(); state.clear()
        absinfo.update(dev.absinfo())
        for code, info in absinfo.items():
            state[code] = info.value
        print(f"Source: {dev.path} {dev.name} [{dev.info.vendor:04x}:{dev.info.product:04x}]"
              "  bytes 0-7 = " + ", ".join(
                  n + ("" if n == "NONE" or AXIS_NAMES[n] in absinfo else "(missing)")
                  for n in names), flush=True)

    def to_byte(code):
        if code is None or code not in absinfo:
            return 128
        info = absinfo[code]
        span = info.max - info.min
        if span <= 0:
            return 128
        v = (state.get(code, info.value) - info.min) * 255 // span
        return max(0, min(255, v))

    if src:
        attach(src)
    else:
        print("Waiting for a joystick ...", flush=True)

    last, last_sent, last_scan = None, 0.0, 0.0
    try:
        while True:
            fds = [fd] + ([src.fd] if src else [])
            r, _, _ = select.select(fds, [], [], 0.01)
            if src and src.fd in r:
                try:
                    for ev in src.read():
                        if ev.type == EV_ABS:
                            state[ev.code] = ev.value
                except BlockingIOError:
                    pass
                except OSError:
                    print("Source disconnected – centring and waiting ...", flush=True)
                    src.close()
                    src = None
                    absinfo.clear(); state.clear()
            if fd in r:
                os.read(fd, UHID_EVENT_SIZE)  # output reports etc. – ignored
            now = time.monotonic()
            if src is None and now - last_scan > 1.0:
                last_scan = now
                src = pick_source(a)
                if src:
                    attach(src)
            report = bytes(to_byte(c) for c in mapping)
            if a.raw_file:
                try:
                    report = bytes.fromhex(open(a.raw_file).read().strip())[:8].ljust(8, b"\x80")
                except (OSError, ValueError):
                    pass
            # Phoenix polls continuously; keep a steady ~100 Hz stream.
            if report != last or now - last_sent > 0.01:
                uhid_input(fd, report)
                if a.verbose and report != last:
                    print(report.hex(" "), flush=True)
                last, last_sent = report, now
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.write(fd, struct.pack("<I", UHID_DESTROY).ljust(UHID_EVENT_SIZE, b"\0"))
        except OSError:
            pass
        os.close(fd)


if __name__ == "__main__":
    main()
