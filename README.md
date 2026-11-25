# Pheonix – Phoenix RC 6.0.i on Linux / Steam Deck

Personal setup for running the abandoned Phoenix RC flight simulator under
Wine (desktop) or Proton (Steam Deck) with any joystick or RC transmitter.

## How it works

Simulator.exe only talks to its own USB dongle (`1781:0898`). It opens the
HID device and reads raw 9-byte reports (report ID + 8 channel bytes).
Ordinary controllers send reports of a different size, so under Wine every
read fails and the sticks never move.

* **`dongle.py`** is a software PhoenixRC dongle. It reads a Linux joystick
  (evdev: Xbox pad, BetaFPV over Bluetooth, EdgeTX radio, ...) and creates a
  virtual `1781:0898` HID device through `/dev/uhid` that sends the dongle's
  8-byte reports. Wine opens it through hidraw. It registers on the virtual
  bus, not USB: Linux's `hid-pxrc` driver (kernel 6.10+) binds to any USB
  `1781:0898` and rewrites bytes 1 and 7 of every report in place, so hidraw
  readers like Wine would see altered data.
* **`launcher.py`** applies the dongle-auth bypass to `Simulator.exe` (from
  `Simulator.exe.bak`, one `jnz` → `nop nop`), starts `dongle.py`, sets up
  Wine's HID bus (Wine 9: SDL off so the udev/hidraw backend runs), launches
  the game, and cleans up afterwards.

Dongle report layout as Phoenix sees it:

| byte | Phoenix channel | default source axis |
|------|-----------------|---------------------|
| 0    | 1               | X                   |
| 1    | (unused)        | –                   |
| 2–6  | 2–6             | Y, RX, RY, Z, RZ    |
| 7    | 7/8 (multiplexed, not fully worked out) | THROTTLE |

Change it with `--map`, e.g. `--map X,NONE,Y,Z,RX,RY,RZ,THROTTLE`.
Channel-to-function assignment happens in Phoenix's own *Setup new
transmitter* wizard.

## Quick start (Steam Deck or any Linux PC)

You need your own copy of these two Phoenix files (they are not in this repo):

* `Phoenix_DVD_5.5.zip` (or the unpacked `Phoenix DVD V5.5` folder)
* `PhoenixRC_5.5.l_to_6.0.i_update.pkg`

Put them in one folder, then:

```
git clone https://github.com/unigamer/pheonix-sim-on-linux.git && cd pheonix-sim-on-linux
./install.sh ~/Downloads/phoenix      # the folder with your two files
./run.sh
```

`install.sh` builds the 6.0.i install straight from the DVD files (no Windows
installer), checks every file against `checksums.sha256`, fetches three
DirectX 9 DLLs from Microsoft's DirectX June 2010 redistributable (checked by
hash), installs OpenAL from the DVD, and adds a "Phoenix RC" menu entry. It
uses Steam's Proton when Steam has one installed, otherwise system Wine.
At the end it runs `sudo ./setup-host.sh` once (udev rule for the software dongle).

**Steam Deck**, in Desktop Mode (Konsole):

1. `passwd` if you have never set a password (sudo needs it).
2. Steam must have a Proton installed: Library → search "Proton" → install
   *Proton 9.0* or newer (any game you've already played on Proton counts).
3. Run the three commands above.
4. Steam → *Add a Non-Steam Game* → tick **Phoenix RC**. In its
   Properties → Controller, set Steam Input to *Disabled*.
5. Pair the transmitter over Bluetooth (or plug it in) *before* starting
   Phoenix; Phoenix only looks for the dongle at startup.

First start: *Setup new transmitter*, moving the sticks in slow circles.

## Using an existing Wine prefix

Without `pheonix.json` (written by `install.sh`) the launcher uses system Wine
and an already-installed 6.0.i prefix in `./prefix`. Run `sudo ./setup-host.sh`
once for the udev rule.

## Running

```
./run.sh                           # first joystick found (non-Xbox preferred)
./run.sh --source-name BETAFPV     # pick a joystick by name
python3 dongle.py --list           # show candidate joysticks
./run.sh --xvfb                    # hidden display (testing)
```

On every start the launcher checks the PhoenixRC install folder against `checksums.sha256`, which takes about 2s. If a file is missing or
changed, it lists the files and stops. Files not in the list, such as models
downloaded later, are ignored. Simulator.exe is checked through its unpatched
copy, `Simulator.exe.bak`.

```
./run.sh --skip-verify             # launch even if the files don't match
python3 launcher.py --write-checksums   # re-record after a deliberate update
```

The launcher turns off Phoenix's startup update check: it sets element 0 of
`options.default_showHelperDialogs` in `Settings/options.sim` to 0, which is
the "Always check for updates when I run Phoenix" box under System → Downloads.
The update server still answers, and its updates have not been tested. It
still lists 58 models and 19 sites, which you can fetch by hand from that
dialog. Pass `--allow-update-check` to leave the setting alone.
`options.sim` is Latin-1 with CRLF line endings. Edit it byte-for-byte:
Phoenix silently resets all options if the encoding changes.

Some stock models crash Phoenix under Wine/Proton as soon as they are
picked. They are listed in `BROKEN_MODELS` in `launcher.py`; so far that is
only the F-117 flying wing, which faults on a bad texture pointer
(`Simulator.exe+0x46CB69`). The launcher moves them out of the install into
`<prefix>/removed-models/` so they don't appear in the model list.
`./run.sh --keep-broken-models` moves them back, for example while working
on a fix.

If Phoenix says "Error initialising DirectX", check `logs/wine.log` for
"The explorer process failed to start". Wine's X11 driver is crashing, and the
launcher already sets `UseXRandR=N` to avoid the known case.

First run with a new controller: Start screen → *Setup new transmitter*.
Move the sticks in slow circles during calibration (instant jumps are not
accepted), then create a control profile.

## Testing tools

These need `python3-evdev` and `python3-xlib`; the main scripts need nothing beyond Python 3.

* `tools/vjoy.py`: virtual 8-axis transmitter via uinput (`--mode circle|step|hold`).
* `tools/drive.py`: X11 screenshot/click/key helper for driving the UI.
* `dongle.py --raw-file f.hex`: send exact report bytes read from a file.
