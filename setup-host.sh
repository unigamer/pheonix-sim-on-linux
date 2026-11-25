#!/usr/bin/env bash
# One-time root setup for the software PhoenixRC dongle (dongle.py).
#   sudo ./setup-host.sh
# Works on Ubuntu and SteamOS (/etc survives SteamOS updates).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"

install -Dm644 udev/70-pheonix.rules /etc/udev/rules.d/70-pheonix.rules
echo uhid > /etc/modules-load.d/pheonix-uhid.conf
modprobe uhid
udevadm control --reload
udevadm trigger --subsystem-match=misc --action=change
udevadm trigger --subsystem-match=hidraw --action=change
udevadm settle
echo "Done. /dev/uhid:"; ls -l /dev/uhid
getfacl -p /dev/uhid 2>/dev/null | grep '^user:' || true
