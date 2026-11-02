#!/usr/bin/env bash
# MedVerify: one command for a whole demonstration.
#
#   bash start_demo.sh            simulated pill box (no ESP32 needed)
#   bash start_demo.sh hardware   real ESP32 pill box (LEDs + buzzer)
#
# Steps: stop leftovers -> start server + camera -> open the three apps -> (optional) camera
# set-up -> guided demo -> leave running or stop. Run it from Git Bash (VS Code terminal:
# "Git Bash"). Every step is a normal `mv` command, so any step can also be run on its own.

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
cd "$ROOT" || exit 1

mv_() { "$PY" "$ROOT/tools/mv.py" "$@"; }
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ask() { local reply; read -r -p "$1 " reply; printf '%s' "$reply"; }
pause() { read -r -p "$1 -- press Enter when done. " _; }

open_url() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) cmd.exe //c start "" "$1" ;;
        Darwin) open "$1" ;;
        *) xdg-open "$1" >/dev/null 2>&1 ;;
    esac
}

if [ ! -x "$PY" ]; then
    echo "Python environment not found (.venv). Create it first:"
    echo "  python -m venv .venv && .venv/Scripts/pip install -r requirements.txt"
    exit 1
fi

MODE="${1:-mock}"
case "$MODE" in
    mock) export MEDVERIFY_DEVICE=mock ;;
    hardware|serial) unset MEDVERIFY_DEVICE; MODE=hardware ;;  # settings.json: serial, port auto
    *) echo "Use: bash start_demo.sh [mock|hardware]"; exit 1 ;;
esac

step "1/6 Stopping anything left running"
mv_ down --force

step "2/6 Starting server and camera ($MODE pill box)"
echo "The webcam turns on and watches the sheet from now until the end."
mv_ up || { echo "Start-up failed: see  $PY tools/mv.py logs server|camera"; exit 1; }

if [ "$MODE" = hardware ]; then
    step "Checking the ESP32"
    mv_ box check || { echo "No pill box found. Check the USB cable (data, not charge-only) and the CP210x driver,"
                       echo "or run the simulation:  bash start_demo.sh"; exit 1; }
    mv_ box test
fi

step "3/6 Opening the apps"
open_url "http://127.0.0.1:5000/console"   # admin console
open_url "http://127.0.0.1:5000/patient"   # patient (father) screen
open_url "http://127.0.0.1:5050/"          # caretaker (son) app: sign in with your password
echo "Admin console : http://127.0.0.1:5000/console"
echo "Patient screen: http://127.0.0.1:5000/patient"
echo "Caretaker app : http://127.0.0.1:5050/"

step "4/6 Camera set-up"
mv_ status
echo
echo "Set up the camera now if this is a new place, the camera or sheet moved, or the pieces changed."
echo "  f = full set-up (squares, empty sheet, loaded sheet)"
echo "  l = loaded photo only (same place, pieces changed)"
echo "  s = skip (calibration above is still right)"
choice="$(ask 'Choice [f/l/s]:')"
case "$choice" in
    f|F)
        echo "Taking one picture to find the drawn squares (nothing is changed)..."
        mv_ cam regions preview
        if [ "$(ask 'All four squares found? [y/n]:')" != y ]; then
            echo "Move the camera so every square is fully in view, then run this script again."; exit 1
        fi
        mv_ cam regions auto || exit 1
        pause "Take ALL pieces off the sheet (keep the sheet and camera still)"
        echo "Taking the empty photo..."
        mv_ cam empty || exit 1
        ;&
    l|L)
        pause "Put the pieces in their squares and take your hands away"
        counts="$(ask 'Pieces per compartment [A=4 B=4 C=4 D=4]:')"
        counts="${counts:-A=4 B=4 C=4 D=4}"
        echo "Taking the loaded photo..."
        # shellcheck disable=SC2086
        mv_ cam loaded $counts || { echo "Fix what it says and run:  $PY tools/mv.py cam loaded $counts"; exit 1; }
        ;;
    *)
        mv_ cam monitor on ;;
esac

step "5/6 Guided demo"
echo "The demo asks you to act (take pieces out, put them back, ...) and checks what the system saw."
echo "Watch the admin console, patient screen and caretaker app while it runs."
if [ "$(ask 'Start the guided demo now? [y/n]:')" = y ]; then
    mv_ demo
fi

step "6/6 Finished"
mv_ status
if [ "$(ask 'Leave everything running for free testing? [y/n]:')" = y ]; then
    echo "Still running. Useful commands (Git Bash):"
    echo "  $PY tools/mv.py status        |  .\\mv status        (PowerShell)"
    echo "  $PY tools/mv.py dose start A pills=1"
    echo "  $PY tools/mv.py down          stop everything when done"
else
    mv_ down
fi
