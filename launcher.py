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
import threading
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
# Stock models that crash Phoenix under Wine/Proton.  F117flyingWing: picking
# it kills Simulator.exe with a page fault at 0x46CB69 (SetTexture on stage 2
# with a texture pointer of 1, with DXVK and wined3d alike).  The launcher
# moves these folders out of the game into <prefix>/removed-models so they
# never show up in the model list; --keep-broken-models moves them back.
BROKEN_MODELS = (
    "resources/objects/aircraft/fixedWing/Electric/F117flyingWing",
)
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


def _removed(rel: str) -> bool:
    return any(rel == m or rel.startswith(m + "/") for m in BROKEN_MODELS)


def set_broken_models(game: Path, prefix: Path, keep: bool) -> None:
    """Move BROKEN_MODELS out of the game (keep=False) or back in (keep=True)."""
    stash = prefix / "removed-models"
    for rel in BROKEN_MODELS:
        live, stashed = game / rel, stash / rel
        src, dst = (stashed, live) if keep else (live, stashed)
        if not src.is_dir():
            continue
        if dst.exists():
            shutil.rmtree(src)  # already a copy where it belongs
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        name = rel.rsplit("/", 1)[-1]
        print(f"Restored model: {name}" if keep else
              f"Removed crashing model: {name} (--keep-broken-models restores it)")


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


def verify_game(game: Path, skip_broken: bool = True) -> list[str]:
    """Return a list of problems (empty = install matches checksums.sha256).

    Files not in the manifest (e.g. models downloaded later) are ignored.
    """
    if not CHECKSUMS.exists():
        return [f"{CHECKSUMS.name} not found"]
    entries = []
    for line in CHECKSUMS.read_text().splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            if not (skip_broken and _removed(rel)):
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


def steam_runtime(proton: Path, steam_root: Path) -> Path | None:
    """The container entry point Proton's toolmanifest asks for.

    Steam never runs Proton bare: it starts it inside a Steam Linux Runtime
    container (Proton 11 -> SteamLinuxRuntime_4).  Run bare, Proton uses the
    host's libraries, and in Game Mode gamescope stopped showing new frames
    after Phoenix's loading screen while the game kept running."""
    try:
        manifest = (proton / "toolmanifest.vdf").read_text()
    except OSError:
        return None
    m = re.search(r'"require_tool_appid"\s+"(\d+)"', manifest)
    if not m:
        return None
    libraries = [steam_root / "steamapps"]
    try:
        vdf = (steam_root / "steamapps/libraryfolders.vdf").read_text()
        libraries += [Path(p) / "steamapps" for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    for lib in libraries:
        try:
            acf = (lib / f"appmanifest_{m.group(1)}.acf").read_text()
        except OSError:
            continue
        d = re.search(r'"installdir"\s+"([^"]+)"', acf)
        entry = lib / "common" / d.group(1) / "_v2-entry-point" if d else None
        if entry and entry.exists():
            return entry
    return None


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
        # In Game Mode Proton starts xalia (gamepad navigation for dialogs).
        # It hooks Phoenix's startup dialogs, and once one is dismissed the
        # game window never gets focus/input, so Phoenix looks frozen.
        env.setdefault("PROTON_USE_XALIA", "0")
        env["WINEPREFIX"] = str(prefix)  # only used for 'wineserver -k'
        env["PHEONIX_WINESERVER"] = str(proton_bin(proton, "wineserver"))
        runtime = None if args.no_runtime else steam_runtime(
            proton, Path(env["STEAM_COMPAT_CLIENT_INSTALL_PATH"]))
        if runtime:
            print(f"Steam Linux Runtime: {runtime.parent.name}")
            return [str(runtime), "--verb=run", "--", str(proton / "proton"), "run"], env
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
; DisableInput turns off the evdev backend, so Phoenix doesn't see ordinary
; gamepads (e.g. Steam Input's virtual "X-Box 360 pad" for the Deck's own
; controls, which can't be switched off).  dongle.py reads those directly
; and our dongle has no evdev node – it reaches Wine through hidraw only.
[HKEY_LOCAL_MACHINE\System\CurrentControlSet\Services\winebus]
"Enable SDL"=dword:00000000
"DisableHidraw"=dword:00000000
"DisableInput"=dword:00000001

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


def in_gamescope() -> bool:
    """True in Steam Deck Game Mode (or any gamescope session)."""
    return (os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"
            or "GAMESCOPE_WAYLAND_DISPLAY" in os.environ)


# Phoenix's display options and the values we force under gamescope.  Phoenix
# normally starts windowed and maximizes its window once the start screen is
# dismissed; gamescope doesn't follow that resize and keeps showing the last
# frame, so the game looks frozen.  Exclusive fullscreen at the size gamescope
# gives the game is what gamescope expects.
GAMESCOPE_DISPLAY = {
    "default_fullscreen": b"1",
    "default_windowState": b"0",
    "default_screenRefresh": b"60",
}
DECK_PANEL = (1280, 800)


def gamescope_size() -> tuple[int, int]:
    """The game's Xwayland screen size in Game Mode.

    That's Steam's per-game "Game Resolution" (e.g. 1280x720 when docked,
    unless set to Native), not the monitor's; gamescope scales it up to the
    output.  Rendering at anything else just gets rescaled again."""
    try:
        out = subprocess.run(["xwininfo", "-root"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"Width: (\d+).*?Height: (\d+)", out, re.S)
        if m:
            return int(m.group(1)), int(m.group(2))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return DECK_PANEL
DESKTOP_DISPLAY_BACKUP = HERE / "logs" / "desktop-display.json"


def _set_option(data: bytes, key: str, value: bytes) -> tuple[bytes, bytes | None]:
    """Replace 'options.<key> = <old>' with value; return (data, old)."""
    pat = re.compile(rb"(options\." + re.escape(key.encode()) + rb" = )(-?\d+)")
    m = pat.search(data)
    if not m:
        return data, None
    return data[:m.start(2)] + value + data[m.end(2):], m.group(2)


def set_display_mode(prefix: Path, gamescope: bool) -> None:
    """Force fullscreen at gamescope's size under gamescope; restore the
    user's own display settings on the next desktop run."""
    forced = dict(GAMESCOPE_DISPLAY)
    if gamescope:
        w, h = gamescope_size()
        forced["default_screenResolution_x"] = str(w).encode()
        forced["default_screenResolution_y"] = str(h).encode()
    for opts in (prefix / "drive_c/users").glob("*/Documents/PhoenixRC/Settings/options.sim"):
        data = opts.read_bytes()  # bytes: Latin-1 + CRLF must survive untouched
        if gamescope:
            saved = {}
            for key, value in forced.items():
                data, old = _set_option(data, key, value)
                if old is not None:
                    saved[key] = old.decode()
            # Keep the first backup: a second Game Mode run would otherwise
            # back up our own forced values.
            if not DESKTOP_DISPLAY_BACKUP.exists():
                DESKTOP_DISPLAY_BACKUP.parent.mkdir(exist_ok=True)
                DESKTOP_DISPLAY_BACKUP.write_text(json.dumps(saved))
            msg = f"Game Mode: Phoenix set to fullscreen {w}x{h}"
        else:
            if not DESKTOP_DISPLAY_BACKUP.exists():
                continue
            for key, value in json.loads(DESKTOP_DISPLAY_BACKUP.read_text()).items():
                data, _ = _set_option(data, key, value.encode())
            msg = "Desktop: restored Phoenix's previous display settings"
        if data != opts.read_bytes():
            opts.write_bytes(data)
            print(msg)
    if not gamescope:
        DESKTOP_DISPLAY_BACKUP.unlink(missing_ok=True)


GAMESCOPE_ROOT_ATOMS = ("GAMESCOPE_FOCUSED_WINDOW", "GAMESCOPE_FOCUSED_APP",
                        "GAMESCOPE_FOCUSED_APP_GFX", "GAMESCOPE_FOCUSABLE_WINDOWS",
                        "GAMESCOPE_FOCUSABLE_APPS", "_NET_ACTIVE_WINDOW")


def _run_text(cmd: list[str], env: dict) -> str:
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<{cmd[0]} failed: {e}>\n"


def _xprops(wid: str, env: dict) -> str:
    props = _run_text(["xprop", "-id", wid, "STEAM_GAME", "WM_CLASS",
                       "_NET_WM_STATE", "WM_TRANSIENT_FOR"], env)
    kept = [p.strip() for p in props.splitlines()
            if "not found" not in p and "no such atom" not in p]
    if not any(p.startswith("STEAM_GAME") for p in kept):
        kept.insert(0, "STEAM_GAME=none")
    return " | ".join(kept)


def _gamescope_snapshot(env: dict, diag: Path | None) -> str:
    """Which window gamescope focuses, and Wine's X windows.

    gamescope publishes its focus atoms on the root of Steam's own Xwayland
    (:0); the game usually runs on a second one (:1), so query both."""
    out = []
    steam_env = dict(env, DISPLAY=":0")
    out.append("[:0 root]\n" + _run_text(["xprop", "-root", *GAMESCOPE_ROOT_ATOMS], steam_env))
    if env.get("DISPLAY") != ":0":
        out.append(f"[{env.get('DISPLAY')} root]\n"
                   + _run_text(["xprop", "-root", *GAMESCOPE_ROOT_ATOMS], env))
    root = _run_text(["xwininfo", "-root"], env)
    size = re.search(r"Width: (\d+).*?Height: (\d+)", root, re.S)
    out.append(f"root size: {size.group(1)}x{size.group(2)}\n" if size else "root size: ?\n")
    tree = _run_text(["xwininfo", "-root", "-children"], env)
    for line in tree.splitlines():
        m = re.match(r"\s+(0x[0-9a-f]+) (.*)", line)
        if not m or "steamwebhelper" in line:
            continue
        wid = m.group(1)
        info = _run_text(["xwininfo", "-id", wid], env)
        state = re.search(r"Map State: (\S+)", info)
        state = state.group(1) if state else "?"
        out.append(f"  {wid} {state} {m.group(2)}\n      {_xprops(wid, env)}\n")
        if state != "IsViewable":
            continue
        # Wine draws Vulkan/DXVK output into a child "client" window; that's
        # the xid gamescope WSI attaches its surface to (see wine.log).
        kids = _run_text(["xwininfo", "-id", wid, "-children"], env)
        for kline in kids.splitlines():
            k = re.match(r"\s+(0x[0-9a-f]+) (.*)", kline)
            if not k:
                continue
            kinfo = _run_text(["xwininfo", "-id", k.group(1)], env)
            kstate = re.search(r"Map State: (\S+)", kinfo)
            out.append(f"      child {k.group(1)} {kstate.group(1) if kstate else '?'} "
                       f"{k.group(2)}\n")
    if diag and diag.exists():
        try:
            lines = diag.read_bytes().splitlines()
            out.append(f"Diagnostic.log: {lines[-1].decode('latin-1').strip() if lines else ''}\n")
        except OSError:
            pass
    return "".join(out)


def gamescope_logger(env: dict, prefix: Path, log: Path, stop: threading.Event) -> None:
    """Every 2 s, append the gamescope/X window state to log when it changes.

    Diagnostic aid for Game Mode, where Phoenix keeps running but gamescope
    keeps showing a stale frame."""
    if not (shutil.which("xprop") and shutil.which("xwininfo")):
        log.write_text("xprop/xwininfo not found – no gamescope logging\n")
        return
    diag = next(iter((prefix / "drive_c/users").glob(
        "*/Documents/PhoenixRC/Diagnostic.log")), None)
    keys = ("DISPLAY", "WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY",
            "XDG_CURRENT_DESKTOP", "SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID",
            "ENABLE_GAMESCOPE_WSI", "PROTON_USE_XALIA")
    t0 = time.monotonic()
    last = None
    with open(log, "w") as f:
        f.write("".join(f"{k}={env.get(k)}\n" for k in keys))
        while not stop.is_set():
            snap = _gamescope_snapshot(env, diag)
            if snap != last:
                f.write(f"\n=== t={time.monotonic() - t0:.1f}s\n{snap}")
                f.flush()
                last = snap
            stop.wait(2)


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
    ap.add_argument("--no-runtime", action="store_true",
                    help="Proton: run it bare instead of inside its Steam Linux Runtime")
    ap.add_argument("--allow-update-check", action="store_true",
                    help="don't untick Phoenix's startup update check")
    ap.add_argument("--display", choices=["auto", "gamescope", "keep"], default="auto",
                    help="auto: fullscreen at gamescope's resolution in Steam Deck Game Mode, your own "
                         "settings elsewhere; gamescope: always force it; keep: don't touch")
    ap.add_argument("--skip-verify", action="store_true",
                    help="run even if the game files don't match checksums.sha256")
    ap.add_argument("--keep-broken-models", action="store_true",
                    help="put back the models the launcher removes because they crash "
                         "Phoenix (see BROKEN_MODELS), e.g. to work on a fix")
    ap.add_argument("--write-checksums", action="store_true",
                    help="record the current install as checksums.sha256 and exit")
    args = ap.parse_args()

    prefix = Path(args.prefix)
    sim = find_simulator(prefix)
    game = sim.parent
    if args.write_checksums:
        write_checksums(game)
        return
    set_broken_models(game, prefix, args.keep_broken_models)
    if args.skip_verify:
        print("Skipping game file verification (--skip-verify)")
    else:
        t0 = time.monotonic()
        problems = verify_game(game, skip_broken=not args.keep_broken_models)
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
    if args.display == "auto":
        set_display_mode(prefix, in_gamescope() and not args.xvfb)
    elif args.display == "gamescope":
        set_display_mode(prefix, True)

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
    stop_log = threading.Event()  # ends the Game Mode window logger

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

        if in_gamescope() and not args.xvfb:
            # PHEONIX_GAMESCOPE_WSI=0 in Steam's launch options presents DXVK
            # frames through plain Xwayland instead of gamescope's Vulkan WSI
            # layer (Steam exports ENABLE_GAMESCOPE_WSI=1 in Game Mode).
            if "PHEONIX_GAMESCOPE_WSI" in os.environ:
                env["ENABLE_GAMESCOPE_WSI"] = os.environ["PHEONIX_GAMESCOPE_WSI"]
            log_env = {k: v for k, v in env.items() if k != "LD_PRELOAD"}
            threading.Thread(target=gamescope_logger, daemon=True,
                             args=(log_env, prefix, logs / "gamescope.log", stop_log)).start()

        print(f"Launching {sim}")
        with open(logs / "wine.log", "w") as wlog:
            game = subprocess.Popen(wine + [str(sim)], cwd=sim.parent, env=env,
                                    stdout=wlog, stderr=subprocess.STDOUT)
            game.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_log.set()
        cleanup()


if __name__ == "__main__":
    main()
