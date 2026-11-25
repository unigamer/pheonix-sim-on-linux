#!/usr/bin/env python3
"""drive.py – tiny X11 remote-control helper for closed-loop testing.

Usage (DISPLAY env or --display):
  drive.py shot out.png            # screenshot whole screen
  drive.py click X Y [button]      # move + click (button 1 default)
  drive.py move X Y
  drive.py key KEYSYM [KEYSYM...]  # e.g. Return Escape Tab alt+F4
  drive.py type "text"
  drive.py windows                 # list top-level windows with geometry
"""
import argparse
import os
import subprocess
import sys
import time

from Xlib import X, XK, display as xdisplay
from Xlib.ext import xtest


def d():
    return xdisplay.Display(os.environ.get("DISPLAY"))


def shot(path):
    subprocess.run(["import", "-window", "root", path], check=True)
    print(path)


def move(dpy, x, y):
    xtest.fake_input(dpy, X.MotionNotify, x=x, y=y)
    dpy.sync()


def click(x, y, button=1):
    dpy = d()
    move(dpy, x, y)
    time.sleep(0.05)
    xtest.fake_input(dpy, X.ButtonPress, button)
    dpy.sync()
    time.sleep(0.08)
    xtest.fake_input(dpy, X.ButtonRelease, button)
    dpy.sync()


def _keycode(dpy, name):
    ks = XK.string_to_keysym(name)
    if ks == 0 and len(name) == 1:
        ks = ord(name)
    kc = dpy.keysym_to_keycode(ks)
    if not kc:
        sys.exit(f"unknown key {name}")
    return kc


def key(combo):
    dpy = d()
    names = combo.split("+")
    mods = {"ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L"}
    codes = [_keycode(dpy, mods.get(n.lower(), n)) for n in names]
    for c in codes:
        xtest.fake_input(dpy, X.KeyPress, c)
        dpy.sync()
        time.sleep(0.03)
    for c in reversed(codes):
        xtest.fake_input(dpy, X.KeyRelease, c)
        dpy.sync()
        time.sleep(0.03)


def type_text(text):
    for ch in text:
        name = {" ": "space", "\n": "Return", ".": "period", "/": "slash",
                "-": "minus", "_": "shift+minus", ":": "shift+colon"}.get(ch, ch)
        if ch.isupper():
            name = "shift+" + ch.lower()
        key(name)


def windows():
    dpy = d()
    root = dpy.screen().root

    def walk(w, depth):
        try:
            attrs = w.get_attributes()
            g = w.get_geometry()
            name = w.get_wm_name()
        except Exception:
            return
        if attrs.map_state == X.IsViewable and name:
            pos = w.translate_coords(root, 0, 0)
            print(f"{w.id:#x} {-pos.x},{-pos.y} {g.width}x{g.height} {name!r}")
        for c in w.query_tree().children:
            walk(c, depth + 1)

    walk(root, 0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--display")
    p.add_argument("cmd")
    p.add_argument("args", nargs="*")
    a = p.parse_args()
    if a.display:
        os.environ["DISPLAY"] = a.display
    if a.cmd == "shot":
        shot(a.args[0] if a.args else "shot.png")
    elif a.cmd == "click":
        click(int(a.args[0]), int(a.args[1]), int(a.args[2]) if len(a.args) > 2 else 1)
    elif a.cmd == "move":
        dpy = d()
        move(dpy, int(a.args[0]), int(a.args[1]))
    elif a.cmd == "key":
        for k in a.args:
            key(k)
            time.sleep(0.1)
    elif a.cmd == "type":
        type_text(" ".join(a.args))
    elif a.cmd == "windows":
        windows()
    else:
        sys.exit(f"unknown cmd {a.cmd}")


if __name__ == "__main__":
    main()
