"""Fake camera: drive the MedVerify backend from the keyboard, without a webcam or pills.

It behaves like vision/camera_inspector.py does over the network: it sends a heartbeat
every few seconds (so the backend sees the camera as online) and posts a confirmed
observation whenever you change a compartment.

    .venv\\Scripts\\python.exe tools\\fake_camera.py            (backend at http://127.0.0.1:5000)

Commands (type and press Enter):
    A removed     compartment A emptied          (REMOVAL_DETECTED)
    A loaded      compartment A full again       (OBJECT_RETURNED)
    A partial     compartment A changed, not empty (PARTIAL_OR_WRONG)
    off / on      stop / resume heartbeats (simulates the camera dying)
    status        print what the fake camera is reporting
    q             quit
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import requests

WORDS = {"removed": "REMOVAL_DETECTED", "empty": "REMOVAL_DETECTED", "loaded": "OBJECT_RETURNED",
         "full": "OBJECT_RETURNED", "partial": "PARTIAL_OR_WRONG"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://127.0.0.1:5000")
    parser.add_argument("--compartments", default="ABCD")
    parser.add_argument("--every", type=float, default=5.0, help="heartbeat interval (s)")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    states = {c: "EXPECTED_PRESENT" for c in args.compartments.upper()}
    alive = threading.Event()
    alive.set()
    stop = threading.Event()
    lock = threading.Lock()

    def post(path: str, payload: dict) -> bool:
        try:
            requests.post(server + path, json=payload, timeout=2).raise_for_status()
            return True
        except requests.RequestException as exc:
            print(f"  ! {path} failed: {exc}")
            return False

    def heartbeat_loop() -> None:
        while not stop.is_set():
            if alive.is_set():
                with lock:
                    snapshot = dict(states)
                post("/api/vision/heartbeat", {"monitoring": True, "camera_index": -1, "states": snapshot})
            stop.wait(args.every)

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    print(f"Fake camera reporting to {server}. All compartments start loaded. Type 'help'.")

    for line in sys.stdin:
        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd in ("q", "quit", "exit"):
            break
        if cmd == "help":
            print(__doc__)
        elif cmd == "off":
            alive.clear()
            print("Heartbeats stopped: the backend will mark the camera offline after its timeout.")
        elif cmd == "on":
            alive.set()
            print("Heartbeats resumed.")
        elif cmd == "status":
            with lock:
                print("  " + "  ".join(f"{c}={s}" for c, s in states.items()), "| heartbeats", "on" if alive.is_set() else "off")
        elif len(parts) == 2 and parts[0].upper() in states and parts[1].lower() in WORDS:
            slot, new = parts[0].upper(), WORDS[parts[1].lower()]
            with lock:
                previous = states[slot]
                states[slot] = new
            if post("/api/observations", {"compartment_id": slot, "state": new, "previous_state": previous,
                                          "changed_pixel_fraction": 0.0, "note": "fake camera"}):
                print(f"  {slot}: {previous} -> {new}")
        else:
            print("  ? Try: 'A removed', 'B loaded', 'C partial', 'off', 'on', 'status', 'q'")
    stop.set()


if __name__ == "__main__":
    main()
