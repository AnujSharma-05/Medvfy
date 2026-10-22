
"""MedVerify: live camera and compartment state-change inspector.

Controls (click the live camera window first):
    R = define or redraw compartments A-D
    S = save the current state as baseline
    V = compare current state with baseline
    Q = quit
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ROI_FILE = CONFIG_DIR / "rois.json"
BASELINE_FILE = DATA_DIR / "vision_baseline.npz"
LOG_FILE = DATA_DIR / "vision_observations.csv"
WINDOW = "MedVerify - Live Camera"


def load_settings():
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}



def open_camera(settings, index=None):
    """Open the selected camera. Index 0 is usually built-in; 1 is often USB."""
    if index is None:
        index = int(settings.get("camera_index", 0))

    index = int(index)
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]

    for backend in backends:
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            cap.set(
                cv2.CAP_PROP_FRAME_WIDTH,
                int(settings.get("camera_width", 1280)),
            )
            cap.set(
                cv2.CAP_PROP_FRAME_HEIGHT,
                int(settings.get("camera_height", 720)),
            )

            # Reject a camera that opens but cannot supply a frame.
            ok, frame = cap.read()
            if ok and frame is not None:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                print(f"Camera {index} opened using backend {backend}.")
                return cap

        cap.release()

    print(f"Could not open camera index {index}.")
    return None


def select_rois(frame, slots):
    """Select all compartments. Cancellation returns safely."""
    chosen = {}

    for slot in slots:
        print(
            f"Select compartment {slot}: drag a box, "
            "press ENTER/SPACE to accept, C to cancel."
        )

        x, y, w, h = map(
            int,
            cv2.selectROI(
                f"Select compartment {slot}",
                frame,
                showCrosshair=True,
                fromCenter=False,
            ),
        )
        cv2.destroyAllWindows()

        if w <= 0 or h <= 0:
            print("Selection cancelled. Returning to live camera.")
            return None

        chosen[slot] = [x, y, w, h]

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ROI_FILE.write_text(json.dumps(chosen, indent=2), encoding="utf-8")

    # A new ROI layout invalidates the old baseline.
    if BASELINE_FILE.exists():
        BASELINE_FILE.unlink()

    print(f"Saved compartment regions to {ROI_FILE}")
    return chosen


def load_rois(slots, frame):
    if not ROI_FILE.exists():
        return None

    try:
        rois = json.loads(ROI_FILE.read_text(encoding="utf-8"))
        height, width = frame.shape[:2]

        for slot in slots:
            if slot not in rois:
                return None

            x, y, w, h = map(int, rois[slot])

            if (
                x < 0 or y < 0 or w <= 0 or h <= 0
                or x + w > width or y + h > height
            ):
                print("Saved regions do not fit this camera image. Press R.")
                return None

            rois[slot] = [x, y, w, h]

        return rois

    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
        return None


def crop_compartments(frame, rois):
    return {
        slot: frame[y:y + h, x:x + w].copy()
        for slot, (x, y, w, h) in rois.items()
    }


def save_baseline(frame, rois):
    crops = crop_compartments(frame, rois)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(BASELINE_FILE, **crops)
    print("Baseline saved. Change a dummy object, then press V.")
    return crops


def load_baseline(slots):
    if not BASELINE_FILE.exists():
        return {}

    try:
        with np.load(BASELINE_FILE, allow_pickle=False) as saved:
            if not all(slot in saved.files for slot in slots):
                return {}
            return {slot: saved[slot].copy() for slot in slots}
    except (ValueError, OSError):
        return {}


def changed_fraction(before, after):
    if before.shape != after.shape:
        after = cv2.resize(after, (before.shape[1], before.shape[0]))

    before = cv2.GaussianBlur(before, (5, 5), 0)
    after = cv2.GaussianBlur(after, (5, 5), 0)

    difference = cv2.absdiff(before, after)
    gray = cv2.cvtColor(difference, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 25, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
    )

    return cv2.countNonZero(mask) / max(1, mask.size)


def log_observations(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_FILE.exists()

    with LOG_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        if new_file:
            writer.writerow([
                "timestamp_utc",
                "compartment_id",
                "state",
                "changed_pixel_fraction",
                "interpretation",
            ])

        writer.writerows(rows)


def send_observation(server_url, slot, state, fraction):
    if not server_url:
        return

    payload = {
        "compartment_id": slot,
        "state": state,
        "changed_pixel_fraction": round(float(fraction), 4),
        "note": "Visual change only; not ingestion verification.",
    }

    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/api/observations",
            json=payload,
            timeout=1,
        )
        response.raise_for_status()
        print(f"  Server: observation for {slot} logged")
    except requests.RequestException:
        print("  Server unavailable; local logging will continue.")


def draw_overlay(frame, rois, latest, message):
    view = frame.copy()

    for slot, (x, y, w, h) in (rois or {}).items():
        state, fraction = latest.get(slot, ("READY", 0.0))

        color = (
            (0, 0, 255) if state == "CHANGED"
            else (0, 200, 0) if state == "NO CHANGE"
            else (0, 200, 255)
        )

        cv2.rectangle(view, (x, y), (x + w, y + h), color, 2)
        cv2.putText(
            view,
            f"{slot}: {state} {fraction:.1%}",
            (x, max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
        )

    cv2.rectangle(view, (0, 0), (view.shape[1], 76), (20, 20, 20), -1)
    cv2.putText(
        view,
        "MedVerify | R: regions  S: baseline  V: compare  Q: quit",
        (12, 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.57,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        view,
        message[:120],
        (12, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (180, 220, 255),
        1,
    )

    return view


def main():
    settings = load_settings()
    slots = [
        str(slot).upper()
        for slot in settings.get("compartments", ["A", "B", "C", "D"])
    ]
    threshold = float(settings.get("change_threshold", 0.08))
    server_url = str(settings.get("server_url", "http://127.0.0.1:5000"))

    camera_index = int(settings.get("camera_index", 0))
    cap = open_camera(settings, camera_index)

    if cap is None:
        print("Connect a camera or change camera_index in config/settings.json.")
        return

    ok, first_frame = cap.read()
    if not ok:
        cap.release()
        print("Camera opened but no frame was available.")
        return

    rois = load_rois(slots, first_frame)
    baseline = load_baseline(slots) if rois else {}
    latest = {}

    message = f"Camera {camera_index} | R: regions C: switch S: baseline V: compare Q: quit"

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    print("\nMedVerify camera inspector")
    print("R = define regions | C = switch camera")
    print("S = save baseline | V = compare | Q = quit")

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                message = "Frame read failed. Press C to try another camera."
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("c"):
                    pass
                else:
                    continue
            else:
                view = draw_overlay(frame, rois, latest, message)
                cv2.imshow(WINDOW, view)
                key = cv2.waitKey(1) & 0xFF

            key = chr(key).lower() if key != 255 else ""

            if key == "q":
                break

            elif key == "c":
                # Try the next few device indexes, cycling through 0-4.
                old_index = camera_index
                cap.release()
                cv2.waitKey(200)

                found = None
                for offset in range(1, 6):
                    candidate = (old_index + offset) % 5
                    trial = open_camera(settings, candidate)
                    if trial is not None:
                        found = (candidate, trial)
                        break

                if found is None:
                    cap = open_camera(settings, old_index)
                    if cap is None:
                        print("No camera could be reopened.")
                        break
                    message = "Could not switch camera; restored previous camera if available."
                    continue

                camera_index, cap = found
                rois = None
                baseline = {}
                latest = {}
                message = (
                    f"Switched to camera {camera_index}. "
                    "Press R to redraw compartment regions."
                )

                # Camera view has changed, so discard the old camera's
                # region selection and baseline.
                for path in (ROI_FILE, BASELINE_FILE):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

                print(f"Switched from camera {old_index} to {camera_index}.")
                print("Redraw your regions using R.")
                continue

            elif key == "r":
                ok, frame = cap.read()
                if not ok:
                    message = "Cannot select regions: frame unavailable."
                    continue

                new_rois = select_rois(frame.copy(), slots)
                if new_rois is not None:
                    rois = new_rois
                    baseline = {}
                    latest = {}
                    message = "Regions saved. Arrange dummy objects, then press S."
                else:
                    message = "Selection cancelled; live feed continues."

            elif key == "s":
                if not rois:
                    message = "Press R to define all compartment regions first."
                    continue

                baseline = save_baseline(frame, rois)
                latest = {s: ("BASELINE SAVED", 0.0) for s in slots}
                message = "Baseline saved. Change a dummy object, then press V."

            elif key == "v":
                if not rois:
                    message = "Press R to define compartment regions first."
                    continue

                if not all(s in baseline for s in slots):
                    message = "No baseline. Place the objects and press S."
                    continue

                current = crop_compartments(frame, rois)
                rows = []
                changed_count = 0
                timestamp = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )

                for slot in slots:
                    fraction = changed_fraction(baseline[slot], current[slot])
                    state = "CHANGED" if fraction >= threshold else "NO CHANGE"
                    latest[slot] = (state, fraction)
                    changed_count += state == "CHANGED"

                    rows.append([
                        timestamp,
                        slot,
                        state,
                        round(fraction, 4),
                        "Visual change only; not proof of dose removal or ingestion",
                    ])

                    print(
                        f"Compartment {slot}: {state} "
                        f"(changed pixels: {fraction:.1%})"
                    )
                    send_observation(server_url, slot, state, fraction)

                log_observations(rows)
                baseline = current
                np.savez_compressed(BASELINE_FILE, **baseline)
                message = (
                    f"Compared {len(slots)} compartments; "
                    f"{changed_count} changed. Baseline updated."
                )

    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
