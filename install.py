#!/usr/bin/env python3
"""install.py – build a Phoenix RC 6.0.i install from your own Phoenix files.

  install.py FOLDER [--runner auto|proton|wine] [--proton DIR] [--force]

FOLDER must contain (anywhere inside, names don't matter):
  * the Phoenix 5.5 DVD: Phoenix_DVD_5.5.zip, or the unpacked
    "Phoenix DVD V5.5" folder
  * the 6.0.i update: PhoenixRC_5.5.l_to_6.0.i_update.pkg

No Windows installer is run.  The DVD keeps the program files unpacked under
files/#INSTALL; the 6.0.i update only replaces Simulator.exe.  The result is
checked file by file against checksums.sha256.

Phoenix also needs three of Microsoft's DirectX 9 DLLs (Wine's own versions
cannot compile its shaders).  They are taken from Microsoft's DirectX June 2010
redistributable, downloaded from microsoft.com unless
directx_Jun2010_redist.exe is already in FOLDER, and checked by hash.

Runner: Steam's Proton if Steam has one installed (Steam Deck), else system
Wine.  The choice is written to pheonix.json for launcher.py.
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import apply_pkg
import launcher

HERE = Path(__file__).resolve().parent
UPDATE_VERSION = "6.0.i"
DVD_INSTALL = "files/#INSTALL/"
DVD_SETTINGS = "files/#SETTINGS/"

DX_REDIST = "directx_Jun2010_redist.exe"
DX_URL = ("https://download.microsoft.com/download/8/4/A/"
          "84A35BF1-DAFE-4AE8-82AF-AD2AE20B6B14/directx_Jun2010_redist.exe")
DX_SHA256 = "053f76dcbb28802e23341b6a787e3b0791c0fa5c8d4d011b1044172dbf89c73b"
# (cab inside the redistributable, file inside that cab, sha256 of the file)
DX_DLLS = [
    ("Oct2006_d3dx9_31_x86.cab", "d3dx9_31.dll",
     "e2065619fe6eb0034833b1dc0369deb4a6edc3110e38a1132eeafcf430c578a5"),
    ("Jun2010_d3dx9_43_x86.cab", "d3dx9_43.dll",
     "0b28546be22c71834501f7d7185ede5d79742457331c7ee09efc14490dd64f5f"),
    ("Jun2010_D3DCompiler_43_x86.cab", "D3DCompiler_43.dll",
     "2f23182ec6f4889397ac4bf03d62536136c5bdba825c7d2c4ef08c827f3a8a1c"),
]
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pheonix"


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- finding the user's Phoenix files ----------------------------------------

class DvdZip:
    def __init__(self, path: Path, root: str):
        self.path, self.root = path, root

    def extract(self, sub: str, dest: Path) -> int:
        n = 0
        with zipfile.ZipFile(self.path) as z:
            for info in z.infolist():
                if not info.filename.startswith(self.root + sub):
                    continue
                target = dest / info.filename[len(self.root + sub):]
                if info.is_dir():
                    # Keep empty folders: Phoenix can't create e.g.
                    # Settings/mappingProfiles/Custom itself and quits when
                    # saving a new profile there.
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                n += 1
        return n


class DvdDir:
    def __init__(self, path: Path):
        self.path = path

    def extract(self, sub: str, dest: Path) -> int:
        src = self.path / sub
        n = sum(1 for p in src.rglob("*") if p.is_file())
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return n


def walk(folder: Path, depth: int):
    """Files and folders up to `depth` levels down, skipping Wine prefixes."""
    for p in sorted(folder.iterdir()):
        yield p
        if p.is_dir() and depth > 1 and not (p / "drive_c").exists() and p.name != "pfx":
            yield from walk(p, depth - 1)


def find_inputs(folder: Path):
    files = [folder] if folder.is_file() else list(walk(folder, 4))
    dvd = pkg = redist = None
    for p in files:
        name = p.name.lower()
        if dvd is None and p.is_file() and name.endswith(".zip"):
            try:
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if n.endswith(DVD_INSTALL + "Simulator.exe"):
                            dvd = DvdZip(p, n[:-len(DVD_INSTALL + "Simulator.exe")])
                            break
            except zipfile.BadZipFile:
                pass
        elif dvd is None and p.is_dir() and (p / DVD_INSTALL / "Simulator.exe").is_file():
            dvd = DvdDir(p)
        elif pkg is None and p.is_file() and name.endswith(".pkg"):
            with open(p, "rb") as f:
                head = f.read(16)
            if head[:1] == b"\x01" and head[3:3 + head[1]] == UPDATE_VERSION.encode():
                pkg = p
        elif redist is None and p.is_file() and name == DX_REDIST.lower():
            redist = p
    missing = []
    if dvd is None:
        missing.append("the Phoenix 5.5 DVD (Phoenix_DVD_5.5.zip or its unpacked folder)")
    if pkg is None:
        missing.append(f"the {UPDATE_VERSION} update (PhoenixRC_5.5.l_to_6.0.i_update.pkg)")
    if missing:
        sys.exit(f"Not found in {folder}:\n  " + "\n  ".join(missing))
    return dvd, pkg, redist


# --- runner -------------------------------------------------------------------

def steam_roots() -> list[Path]:
    home = Path.home()
    roots = []
    for r in (home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
              home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
        if (r / "steamapps").is_dir() and r.resolve() not in [x.resolve() for x in roots]:
            roots.append(r)
    return roots


def find_proton() -> tuple[Path, Path] | None:
    """Best installed Proton: newest numbered Valve Proton, then Experimental, then others."""
    found = []
    for root in steam_roots():
        libs = {root}
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            libs |= {Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))}
        dirs = [d for lib in libs for d in (lib / "steamapps/common").glob("Proton*")]
        dirs += list((root / "compatibilitytools.d").glob("*"))
        for d in dirs:
            if (d / "proton").is_file():
                m = re.fullmatch(r"Proton (\d+)\.(\d+)", d.name)
                rank = ((2, int(m[1]), int(m[2])) if m
                        else (1, 0, 0) if d.name == "Proton - Experimental" else (0, 0, 0))
                found.append((rank, d, root))
    if not found:
        return None
    _, proton, root = max(found, key=lambda t: t[0])
    return proton, root


def pick_runner(args) -> dict:
    if args.runner in ("auto", "proton"):
        if args.proton:
            roots = steam_roots()
            return {"runner": "proton", "proton": str(Path(args.proton).resolve()),
                    "steam_root": str(roots[0] if roots else Path.home() / ".local/share/Steam")}
        hit = find_proton()
        if hit:
            return {"runner": "proton", "proton": str(hit[0]), "steam_root": str(hit[1])}
        if args.runner == "proton":
            sys.exit("No Proton found. In Steam: Library → search 'Proton' → install "
                     "'Proton 9.0' (or newer), then run this again.")
    if shutil.which("wine"):
        return {"runner": "wine"}
    sys.exit("Neither Steam's Proton nor Wine was found.\n"
             "  Steam Deck / Steam users: in Steam, Library → search 'Proton' → install "
             "'Proton 9.0' (or newer).\n"
             "  Otherwise install Wine (e.g. sudo apt install wine).")


# --- DirectX DLLs (libarchive via ctypes: no cabextract on SteamOS) ------------

def _libarchive():
    name = ctypes.util.find_library("archive") or "libarchive.so.13"
    la = ctypes.CDLL(name)
    vp, cp = ctypes.c_void_p, ctypes.c_char_p
    la.archive_read_new.restype = vp
    for f in ("archive_read_support_format_cab", "archive_read_free"):
        getattr(la, f).argtypes = [vp]
    la.archive_read_open_memory.argtypes = [vp, cp, ctypes.c_size_t]
    la.archive_read_next_header.argtypes = [vp, ctypes.POINTER(vp)]
    la.archive_entry_pathname.argtypes = [vp]
    la.archive_entry_pathname.restype = cp
    la.archive_read_data.argtypes = [vp, vp, ctypes.c_size_t]
    la.archive_read_data.restype = ctypes.c_ssize_t
    la.archive_error_string.argtypes = [vp]
    la.archive_error_string.restype = cp
    return la


def cab_extract(la, cab: bytes, want: str) -> bytes:
    a = la.archive_read_new()
    try:
        la.archive_read_support_format_cab(a)
        if la.archive_read_open_memory(a, cab, len(cab)) != 0:
            raise RuntimeError(la.archive_error_string(a))
        entry = ctypes.c_void_p()
        while la.archive_read_next_header(a, ctypes.byref(entry)) == 0:
            if la.archive_entry_pathname(entry).decode().lower() != want.lower():
                continue
            out, buf = bytearray(), ctypes.create_string_buffer(1 << 16)
            while (n := la.archive_read_data(a, buf, len(buf))) > 0:
                out += buf.raw[:n]
            if n < 0:
                raise RuntimeError(la.archive_error_string(a))
            return bytes(out)
        raise RuntimeError(f"{want} not found in cabinet")
    finally:
        la.archive_read_free(a)


def get_redist(given: Path | None) -> bytes:
    for p in (given, CACHE / DX_REDIST):
        if p is None or not p.is_file():
            continue
        data = p.read_bytes()
        if sha256(data) == DX_SHA256:
            return data
        print(f"   ignoring {p}: not the expected file (sha256 mismatch)")
    step("Downloading Microsoft DirectX June 2010 redistributable (96 MB)")
    with urllib.request.urlopen(DX_URL, timeout=60) as r:
        data = r.read()
    if sha256(data) != DX_SHA256:
        sys.exit(f"Download from {DX_URL} has the wrong checksum – try again later.")
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / DX_REDIST).write_bytes(data)
    return data


def install_dx_dlls(game: Path, redist: Path | None) -> None:
    targets = [(game / dll.lower(), digest) for _, dll, digest in DX_DLLS]
    if all(p.is_file() and sha256(p.read_bytes()) == d for p, d in targets):
        print("   DirectX DLLs already present")
        return
    data = get_redist(redist)
    try:
        la = _libarchive()
    except OSError:
        sys.exit("libarchive not found (needed to unpack the DirectX cabinets).\n"
                 "  Ubuntu/Debian: sudo apt install libarchive13")
    outer = data[data.index(b"MSCF\0\0\0\0"):]  # the .exe wraps a plain cabinet
    for cab, dll, digest in DX_DLLS:
        blob = cab_extract(la, cab_extract(la, outer, cab), dll)
        if sha256(blob) != digest:
            sys.exit(f"{dll} from {cab} has an unexpected checksum")
        (game / dll.lower()).write_bytes(blob)
        print(f"   {dll.lower()}")


# --- prefix ---------------------------------------------------------------------

def prefix_user(prefix: Path, runner: str) -> Path:
    users = prefix / "drive_c/users"
    if runner == "proton":
        return users / "steamuser"
    name = os.environ.get("USER") or os.getlogin()
    if (users / name).is_dir():
        return users / name
    others = [u for u in users.iterdir() if u.is_dir() and u.name != "Public"]
    if not others:
        sys.exit(f"no user folder in {users}")
    return others[0]


def run_windows(cfg: dict, prefix: Path, cmd: list[str], cwd: Path | None = None):
    args = argparse.Namespace(runner=cfg["runner"], proton=cfg.get("proton"),
                              steam_root=cfg.get("steam_root"), debug=False)
    wine, env = launcher.wine_env(args, prefix)
    if cfg["runner"] == "wine":
        env["WINEDLLOVERRIDES"] += ";mscoree,mshtml="  # skip Mono/Gecko prompts
    r = subprocess.run(wine + cmd, env=env, cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    launcher.wineserver(env, "-w")
    return r


def init_prefix(cfg: dict, prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    r = run_windows(cfg, prefix, ["wineboot", "-u"])
    if not (prefix / "drive_c/windows").is_dir():
        print(r.stdout[-3000:])
        sys.exit(f"Creating the Wine prefix in {prefix} failed")


def write_desktop_entry(icon: Path | None) -> Path:
    apps = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "pheonix.desktop"
    run = str(HERE / "run.sh")
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Phoenix RC\n"
        "Comment=Phoenix RC flight simulator (Wine/Proton + software dongle)\n"
        f'Exec="{run}"\nPath={HERE}\n'
        + (f"Icon={icon}\n" if icon else "")
        + "Terminal=false\nCategories=Game;Simulation;\n")
    return entry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="folder with your Phoenix DVD zip and 6.0.i .pkg")
    ap.add_argument("--runner", choices=["auto", "proton", "wine"], default="auto")
    ap.add_argument("--proton", help="use this Proton folder (contains the 'proton' script)")
    ap.add_argument("--prefix", type=Path, help="where to create the Wine prefix "
                    "(default: ./proton/pfx or ./prefix)")
    ap.add_argument("--force", action="store_true", help="re-copy the game files")
    a = ap.parse_args()

    folder = a.folder.expanduser().resolve()
    if not folder.exists():
        sys.exit(f"{folder} does not exist")
    dvd, pkg, redist = find_inputs(folder)
    print(f"DVD:    {dvd.path}\nUpdate: {pkg}")

    cfg = pick_runner(a)
    prefix = (a.prefix.expanduser().resolve() if a.prefix
              else HERE / ("proton/pfx" if cfg["runner"] == "proton" else "prefix"))
    cfg["prefix"] = str(prefix)
    print(f"Runner: {cfg['runner']}" + (f" ({cfg['proton']})" if "proton" in cfg else ""))
    print(f"Prefix: {prefix}")

    if not (prefix / "drive_c/windows").is_dir():
        step("Creating the Wine prefix (takes a minute)")
        init_prefix(cfg, prefix)

    drive_c = prefix / "drive_c"
    game = next((drive_c / d for d in launcher.GAME_DIRS if (drive_c / d / "Simulator.exe").exists()),
                drive_c / ("Program Files (x86)" if (drive_c / "Program Files (x86)").is_dir()
                           else "Program Files") / "PhoenixRC")
    problems = ["not installed"] if a.force or not (game / "Simulator.exe").exists() else None
    if problems is None:
        step("Checking existing game files")
        problems = launcher.verify_game(game)
    if problems:
        step(f"Copying game files from the DVD to {game}")
        print(f"   {dvd.extract(DVD_INSTALL, game)} files")
        step(f"Applying the {UPDATE_VERSION} update")
        version, entries = apply_pkg.read_pkg(str(pkg))
        for name, data in entries:
            # 'resources' is a packed archive that collides with the resources/
            # folder; the working install never had it, so leave it out.
            if (game / name).is_dir():
                continue
            (game / name).write_bytes(data)
            print(f"   {name}")
        (game / "PhoenixRC.ver").write_bytes(version.encode())
        (game / "Simulator.exe.bak").unlink(missing_ok=True)  # re-made from the new exe
        step("Checking game files against checksums.sha256")
        problems = launcher.verify_game(game)
    docs = prefix_user(prefix, cfg["runner"]) / "Documents/PhoenixRC"
    if not docs.exists():
        step(f"Creating default settings in {docs}")
        dvd.extract(DVD_SETTINGS, docs)

    if problems:
        print("\n".join("   " + p for p in problems[:20]))
        sys.exit("The game files don't match the known-good 6.0.i install. "
                 "Are your DVD zip and .pkg complete?")
    print("   all files match")

    step("Installing DirectX 9 DLLs")
    install_dx_dlls(game, redist)

    # Creative's OpenAL runtime (wrap_oal.dll) gives Phoenix its sound; the
    # installer ships on the DVD.
    windir = drive_c / "windows" / ("syswow64" if (drive_c / "windows/syswow64").is_dir()
                                    else "system32")
    if not (windir / "wrap_oal.dll").exists():
        step("Installing OpenAL (from the DVD)")
        oal = game / "utilities/OpenAL"
        run_windows(cfg, prefix, ["OpenALinstaller.exe", "/s"], cwd=oal)
        if not (windir / "wrap_oal.dll").exists():
            print("   WARNING: OpenAL did not install – Phoenix may have no sound")

    icon = None
    if isinstance(dvd, DvdZip):
        with zipfile.ZipFile(dvd.path) as z:
            if dvd.root + "PXICON.ico" in z.namelist():
                icon = HERE / "phoenix.ico"
                icon.write_bytes(z.read(dvd.root + "PXICON.ico"))
    elif (dvd.path / "PXICON.ico").exists():
        icon = HERE / "phoenix.ico"
        shutil.copy(dvd.path / "PXICON.ico", icon)

    launcher.CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    entry = write_desktop_entry(icon)
    print(f"\nInstalled. Settings: {launcher.CONFIG}\nMenu entry: {entry}")


if __name__ == "__main__":
    main()
