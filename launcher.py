#!/usr/bin/env python3
"""launcher.py – run Phoenix RC 6.0.i under Wine (or Proton) with any joystick.

  1. Patches Simulator.exe (from Simulator.exe.bak) with the dongle-auth bypass.
  2. Starts dongle.py: a virtual PhoenixRC USB dongle (1781:0898) fed from a
     Linux joystick (Xbox pad, BetaFPV over Bluetooth, ...).
  3. Configures winebus so Wine reads the dongle through hidraw.
  4. Launches Simulator.exe and cleans up when it exits.

Before any of that it checks the install against checksums.sha256 and stops
on a mismatch (--skip-verify to run anyway).

Runner, Proton and prefix come from pheonix.json (written by install.py);
without it: system Wine and ./prefix.

Usage:
  launcher.py [--xvfb] [--runner wine|proton] [--source-name NAME] [--debug]
              [--skip-verify] [--write-checksums]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The auth check (cmp ecx,eax; jnz) – NOPing the jnz accepts any device that
# answers on the dongle's VID/PID, i.e. our virtual dongle.
PATCH_AUTH_FIND = bytes([0x3B, 0xC8, 0x75, 0x27])
PATCH_AUTH_REPLACE = bytes([0x3B, 0xC8, 0x90, 0x90])
DONGLE_ID = "vid_1781&pid_0898"
CHECKSUMS = HERE / "checksums.sha256"
CONFIG = HERE / "pheonix.json"
GAME_DIRS = ("Program Files (x86)/PhoenixRC", "Program Files/PhoenixRC")
# Microsoft's DirectX 9 helpers (install.py puts them next to Simulator.exe).
# Wine's builtin d3dx9 can't compile Phoenix's shaders.
NATIVE_DLLS = ("d3dx9_31", "d3dx9_43", "d3dcompiler_43")


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except FileNotFoundError:
        return {}


def find_simulator(prefix: Path) -> Path:
    for d in GAME_DIRS:
        p = prefix / "drive_c" / d / "Simulator.exe"
        if p.exists():
            return p
    hits = [h for h in (prefix / "drive_c").rglob("Simulator.exe") if "_backup" not in str(h)]
    if not hits:
        sys.exit(f"Simulator.exe not found under {prefix}")
    return hits[0]


def patch_exe(exe: Path) -> None:
    backup = exe.with_suffix(".exe.bak")
    if not backup.exists():
        shutil.copy2(exe, backup)
    data = bytearray(backup.read_bytes())
    if DONGLE_ID.encode() not in data:
        sys.exit("Simulator.exe.bak is not a stock 6.0.i binary (dongle VID/PID missing)")
    n = data.count(PATCH_AUTH_FIND)
    if n != 1:
        sys.exit(f"auth pattern found {n} times (expected 1) – is this 6.0.i?")
    data = data.replace(PATCH_AUTH_FIND, PATCH_AUTH_REPLACE)
    if exe.read_bytes() != data:
        exe.write_bytes(data)
        print("Patched Simulator.exe (auth bypass)")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _stock_path(game: Path, rel: str) -> Path:
    # The manifest lists the *unpatched* Simulator.exe; once patched, the
    # stock copy lives in Simulator.exe.bak.
    if rel == "Simulator.exe" and (game / "Simulator.exe.bak").exists():
        return game / "Simulator.exe.bak"
    return game / rel


def write_checksums(game: Path) -> None:
    rels = sorted(str(p.relative_to(game)) for p in game.rglob("*")
                  if p.is_file() and p.name != "Simulator.exe.bak"
                  and not (p.name == "Simulator.exe" and p.parent == game
                           and (game / "Simulator.exe.bak").exists()))
    if "Simulator.exe" not in rels:
        rels.append("Simulator.exe")
        rels.sort()
    with ThreadPoolExecutor() as pool:
        sums = list(pool.map(lambda r: _sha256(_stock_path(game, r)), rels))
    CHECKSUMS.write_text("".join(f"{h}  {r}\n" for h, r in zip(sums, rels)))
    print(f"Wrote {len(rels)} checksums to {CHECKSUMS}")


def verify_game(game: Path) -> list[str]:
    """Return a list of problems (empty = install matches checksums.sha256).

    Files not in the manifest (e.g. models downloaded later) are ignored.
    """
    if not CHECKSUMS.exists():
        return [f"{CHECKSUMS.name} not found"]
    entries = []
    for line in CHECKSUMS.read_text().splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            entries.append((digest, rel))

    def check(entry):
        digest, rel = entry
        path = _stock_path(game, rel)
        if not path.is_file():
            return f"missing:  {rel}"
        if _sha256(path) != digest:
            return f"modified: {rel}"
        return None

    with ThreadPoolExecutor() as pool:
        return [p for p in pool.map(check, entries) if p]


def proton_bin(proton: Path, name: str) -> Path:
    for sub in ("files/bin", "dist/bin"):
        if (proton / sub / name).exists():
            return proton / sub / name
    return proton / "files/bin" / name


def wine_env(args, prefix: Path) -> tuple[list[str], dict]:
    """Command prefix to run a Windows program, and its environment."""
    env = dict(os.environ)
    env.setdefault("WINEDEBUG", "+hid" if args.debug else "-all")
    overrides = ",".join(NATIVE_DLLS) + "=n,b"
    env["WINEDLLOVERRIDES"] = ";".join(filter(None, [env.get("WINEDLLOVERRIDES"), overrides]))
    if args.runner == "proton":
        proton = Path(args.proton or os.environ.get("PROTON", ""))
        if not (proton / "proton").exists():
            sys.exit("--runner proton needs --proton DIR (folder containing the 'proton' script)")
        if prefix.name != "pfx":
            sys.exit(f"Proton prefixes must be called 'pfx' (got {prefix})")
        env["STEAM_COMPAT_DATA_PATH"] = str(prefix.parent)
        env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH",
                       args.steam_root or str(Path.home() / ".local/share/Steam"))
        # Proton: let winebus open our dongle's hidraw node.
        env["PROTON_ENABLE_HIDRAW"] = "0x1781/0x0898"
        env["WINEPREFIX"] = str(prefix)  # only used for 'wineserver -k'
        env["PHEONIX_WINESERVER"] = str(proton_bin(proton, "wineserver"))
        return [str(proton / "proton"), "run"], env
    env["WINEPREFIX"] = str(prefix)
    env["PHEONIX_WINESERVER"] = "wineserver"
    return ["wine"], env


def wineserver(env: dict, *args: str) -> None:
    subprocess.run([env["PHEONIX_WINESERVER"], *args], env=env, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def win_path(path: Path) -> str:
    return "Z:" + str(path).replace("/", "\\")


REG_SETTINGS = r"""Windows Registry Editor Version 5.00

; Wine 9: the udev (hidraw) backend only runs when SDL is disabled.
[HKEY_LOCAL_MACHINE\System\CurrentControlSet\Services\winebus]
"Enable SDL"=dword:00000000
"DisableHidraw"=dword:00000000
"DisableInput"=dword:00000000

; Wine 9's XRandR 1.4 code crashes explorer.exe on some hybrid Intel/NVIDIA
; X setups ("The explorer process failed to start" -> Phoenix reports "Error
; initialising DirectX").  Phoenix runs windowed, so the Xinerama fallback is fine.
[HKEY_CURRENT_USER\Software\Wine\X11 Driver]
"UseXRandR"="N"

[HKEY_CURRENT_USER\Software\Wine\DllOverrides]
""" + "".join(f'"{d}"="native,builtin"\n' for d in NATIVE_DLLS)


def configure_wine(wine: list[str], env: dict, logs: Path) -> None:
    reg = logs / "pheonix.reg"
    reg.write_text(REG_SETTINGS.replace("\n", "\r\n"), encoding="utf-16")
    subprocess.run(wine + ["regedit", "/S", win_path(reg)], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    # winebus reads its options at startup – restart the wineserver.
    wineserver(env, "-k")
    time.sleep(1)


def start_xvfb() -> tuple[subprocess.Popen, str]:
    n = 77
    while Path(f"/tmp/.X11-unix/X{n}").exists():
        n += 1
    proc = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    return proc, f":{n}"


def disable_update_check(prefix: Path) -> None:
    """Untick "Always check for updates when I run Phoenix".

    Phoenix stores it as element 0 of options.default_showHelperDialogs in
    Documents/PhoenixRC/Settings/options.sim (the update server is still live
    and offers untested updates at every start).
    """
    for opts in (prefix / "drive_c/users").glob("*/Documents/PhoenixRC/Settings/options.sim"):
        # Edit as bytes: the file is Latin-1 with CRLF line endings and Phoenix
        # rejects it (resetting all options) if either is changed.
        data = opts.read_bytes()
        new = re.sub(rb"(options\.default_showHelperDialogs = \{\s*)1\b", rb"\g<1>0", data)
        if new != data:
            opts.write_bytes(new)
            print(f"Disabled startup update check in {opts}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg = load_config()
    ap.add_argument("--prefix", default=os.environ.get("PHEONIX_PREFIX",
                                                       cfg.get("prefix", str(HERE / "prefix"))))
    ap.add_argument("--runner", choices=["wine", "proton"], default=cfg.get("runner", "wine"))
    ap.add_argument("--proton", default=cfg.get("proton"),
                    help="Proton install dir (for --runner proton)")
    ap.add_argument("--steam-root", default=cfg.get("steam_root"), help=argparse.SUPPRESS)
    ap.add_argument("--xvfb", action="store_true", help="run on a hidden Xvfb display")
    ap.add_argument("--source-name", help="joystick name substring for dongle.py")
    ap.add_argument("--map", help="axis map passed to dongle.py")
    ap.add_argument("--no-dongle", action="store_true", help="don't start dongle.py")
    ap.add_argument("--debug", action="store_true", help="WINEDEBUG=+hid")
    ap.add_argument("--allow-update-check", action="store_true",
                    help="don't untick Phoenix's startup update check")
    ap.add_argument("--skip-verify", action="store_true",
                    help="run even if the game files don't match checksums.sha256")
    ap.add_argument("--write-checksums", action="store_true",
                    help="record the current install as checksums.sha256 and exit")
    args = ap.parse_args()

    prefix = Path(args.prefix)
    sim = find_simulator(prefix)
    game = sim.parent
    if args.write_checksums:
        write_checksums(game)
        return
    if args.skip_verify:
        print("Skipping game file verification (--skip-verify)")
    else:
        t0 = time.monotonic()
        problems = verify_game(game)
        if problems:
            for p in problems[:20]:
                print(f"  {p}", file=sys.stderr)
            if len(problems) > 20:
                print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
            sys.exit(f"Game files in {game} don't match {CHECKSUMS.name} "
                     f"({len(problems)} problem(s)). Halting.\n"
                     "Run with --skip-verify to launch anyway.")
        print(f"Verified game files ({time.monotonic() - t0:.1f}s)")
    patch_exe(sim)
    if not args.allow_update_check:
        disable_update_check(prefix)

    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    children: list[subprocess.Popen] = []
    kill_wine: list[dict] = []  # env for 'wineserver -k' once wine was started

    def cleanup(*_):
        for env in kill_wine:
            wineserver(env, "-k")
        for c in reversed(children):
            if c.poll() is None:
                c.terminate()
        for c in children:
            try:
                c.wait(timeout=5)
            except subprocess.TimeoutExpired:
                c.kill()

    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))

    try:
        if not args.no_dongle:
            cmd = [sys.executable, str(HERE / "dongle.py"), "--wait"]
            if args.source_name:
                cmd += ["--source-name", args.source_name]
            if args.map:
                cmd += ["--map", args.map]
            dongle = subprocess.Popen(cmd, stdout=open(logs / "dongle.log", "w"),
                                      stderr=subprocess.STDOUT)
            children.append(dongle)
            time.sleep(1.0)
            if dongle.poll() is not None:
                print((logs / "dongle.log").read_text(), file=sys.stderr)
                sys.exit("dongle.py failed to start")
            print((logs / "dongle.log").read_text().strip())

        env_display = None
        if args.xvfb:
            xvfb, env_display = start_xvfb()
            children.insert(0, xvfb)
            print(f"Xvfb on {env_display}")

        wine, env = wine_env(args, prefix)
        if env_display:
            env["DISPLAY"] = env_display
        kill_wine.append(env)
        configure_wine(wine, env, logs)

        print(f"Launching {sim}")
        with open(logs / "wine.log", "w") as wlog:
            game = subprocess.Popen(wine + [str(sim)], cwd=sim.parent, env=env,
                                    stdout=wlog, stderr=subprocess.STDOUT)
            game.wait()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
