#!/usr/bin/env python3
"""vjoy.py – virtual RC-transmitter-style joystick via /dev/uinput (for testing).

Creates an 8-axis joystick (like an EdgeTX/ELRS radio in USB/BLE joystick mode)
and sweeps the sticks so input can be verified without touching hardware.

  vjoy.py [--vid 1209 --pid 4f54] [--seconds 60] [--mode circle|centre|hold]
          [--hold ch=value,...]   # value -1.0..1.0, ch 0..7
"""
import argparse
import math
import time

from evdev import AbsInfo, UInput, ecodes as e

AXES = [e.ABS_X, e.ABS_Y, e.ABS_Z, e.ABS_RX, e.ABS_RY, e.ABS_RZ,
        e.ABS_THROTTLE, e.ABS_RUDDER]
LO, HI = 0, 2047


def scale(v):
    return int(LO + (v + 1) / 2 * (HI - LO))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--vid", default="1209")
    p.add_argument("--pid", default="4f54")
    p.add_argument("--name", default="Virtual RC Transmitter")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--mode", default="circle", choices=["circle", "centre", "hold", "step"])
    p.add_argument("--hold", default="")
    a = p.parse_args()

    caps = {
        e.EV_ABS: [(ax, AbsInfo(value=scale(0), min=LO, max=HI, fuzz=0, flat=0,
                                resolution=0)) for ax in AXES],
        e.EV_KEY: [e.BTN_TRIGGER, e.BTN_THUMB, e.BTN_THUMB2, e.BTN_TOP],
    }
    ui = UInput(caps, name=a.name, vendor=int(a.vid, 16), product=int(a.pid, 16),
                version=0x0100, bustype=e.BUS_USB)
    print(f"created {ui.device.path} {a.vid}:{a.pid}", flush=True)

    hold = {}
    for kv in filter(None, a.hold.split(",")):
        k, v = kv.split("=")
        hold[int(k)] = float(v)

    t0 = time.time()
    try:
        while time.time() - t0 < a.seconds:
            t = time.time() - t0
            for i, ax in enumerate(AXES):
                if a.mode == "circle":
                    v = math.sin(t * 1.5 + (math.pi / 2 if i % 2 else 0) + i * 0.3)
                elif a.mode == "step":
                    # axis k = int(t/4) % 8 goes to +1, all others centred
                    v = 1.0 if i == int(t / 4) % len(AXES) else 0.0
                elif a.mode == "hold":
                    v = hold.get(i, 0.0)
                else:
                    v = 0.0
                ui.write(e.EV_ABS, ax, scale(v))
            ui.syn()
            time.sleep(0.02)
    finally:
        ui.close()


if __name__ == "__main__":
    main()
