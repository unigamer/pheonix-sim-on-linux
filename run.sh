#!/usr/bin/env bash
# Start Phoenix RC with the software dongle.  Extra args go to launcher.py,
# e.g.  ./run.sh --source-name BETAFPV    ./run.sh --xvfb
cd "$(dirname "$(readlink -f "$0")")" && exec python3 launcher.py "$@"
