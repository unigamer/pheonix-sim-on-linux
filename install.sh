#!/usr/bin/env bash
# One-command setup:   ./install.sh ~/Downloads/phoenix
# (folder holding your Phoenix_DVD_5.5.zip and PhoenixRC_5.5.l_to_6.0.i_update.pkg)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 FOLDER   (FOLDER holds Phoenix_DVD_5.5.zip and the 6.0.i .pkg)" >&2
    exit 1
fi

python3 install.py "$@"

# The software dongle needs /dev/uhid; that takes root once.
if [[ ! -w /dev/uhid ]] || ! cmp -s udev/70-pheonix.rules /etc/udev/rules.d/70-pheonix.rules; then
    echo
    echo "==> One-time root step: let your user create the virtual Phoenix dongle"
    if ! passwd -S "$USER" 2>/dev/null | grep -qE "^$USER P"; then
        echo "    (Steam Deck: if you never set a password, cancel with Ctrl+C,"
        echo "     run 'passwd' to set one, then run this script again.)"
    fi
    sudo ./setup-host.sh
fi

cat <<EOF

All set. Start Phoenix with:   $PWD/run.sh
Steam / Steam Deck: Steam → Games → Add a Non-Steam Game → tick "Phoenix RC".
Pair or plug in your transmitter before starting. First time in Phoenix:
choose "Setup new transmitter" and move the sticks in slow circles.
EOF
