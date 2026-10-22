"""MedVerify: live camera and compartment state-change inspector.

Controls (click the live camera window first):
    R = define or redraw compartments A-D
    S = save current state as EXPECTED medication baseline
    E = save current state as EMPTY compartment baseline
    V = compare current observation against baseline (once)
    M = start/stop continuous monitoring
    C = switch camera
    Q = quit

SAFETY NOTICE:
Visual observation of compartment emptiness is NOT proof that medicine was swallowed.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from vision.comparator import VisualComparator
from vision.event_logger import EventLogger
from vision.frame_quality import FrameQualityAssessor
from vision.monitor import EVALUATED, WAITING, ContinuousMonitor, MonitorConfig
from vision.reference_store import ReferenceStore
from vision.roi_manager import ROIManager
from vision.state_machine import CompartmentState, CompartmentStateMachine, StateTransition
from vision.trials import TrialRecorder

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ROI_FILE = CONFIG_DIR / "rois.json"
BASELINE_FILE = DATA_DIR / "vision_baseline.npz"
STATE_FILE = DATA_DIR / "vision_state.json"
LOG_FILE = DATA_DIR / "vision_observations.csv"
WINDOW = "MedVerify - Live Camera"


def load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def open_camera(settings: dict[str, Any], index: int | None = None) -> cv2.VideoCapture | None:
    """Open the selected camera using Windows DirectShow with fallback to MSMF / ANY."""
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

            # Reject a camera that opens but cannot supply a valid frame
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                print(f"Camera {index} opened using backend {backend}.")
                warm_up(cap)
                return cap

        cap.release()

    print(f"Could not open camera index {index}.")
    return None


def warm_up(cap: Any, max_seconds: float = 3.0, min_brightness: float = 20.0, stable_frames: int = 5) -> bool:
    """Discard frames until auto-exposure settles (several bright frames in a row).

    Webcams deliver near-black frames for the first ~0.5-1 s; evaluating those would report
    every compartment as UNCERTAIN at start-up. Returns False if exposure never settled.
    """
    deadline = time.monotonic() + max_seconds
    in_a_row = 0
    while time.monotonic() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None and frame.size and float(np.mean(frame)) >= min_brightness:
            in_a_row += 1
            if in_a_row >= stable_frames:
                return True
        else:
            in_a_row = 0
    print("Camera image stayed dark during warm-up: check the lens cover and the room light.")
    return False


def select_rois(
    frame: np.ndarray,
    slots: list[str],
    ref_store: ReferenceStore | None = None,
    camera_index: int | None = None,
) -> dict[str, list[int]] | None:
    """Interactive ROI selection with cancellation safety.

    ref_store must be the same store instance the caller compares against: clearing a
    separate instance only deletes the files and leaves the caller's in-memory
    references stale, so the next comparison would use crops from the old layout.
    """
    chosen: dict[str, list[int]] = {}

    for slot in slots:
        print(
            f"Select compartment {slot}: drag a box, "
            "press ENTER/SPACE to accept, C to cancel."
        )

        box = cv2.selectROI(
            f"Select compartment {slot}",
            frame,
            showCrosshair=True,
            fromCenter=False,
        )
        cv2.destroyAllWindows()

        x, y, w, h = map(int, box)
        if w <= 0 or h <= 0:
            print("Selection cancelled. Returning to live camera.")
            return None

        chosen[slot] = [x, y, w, h]

    manager = ROIManager(ROI_FILE)
    manager.save(chosen, frame.shape, camera_index=camera_index)

    # Invalidate references because layout changed
    store = ref_store if ref_store is not None else ReferenceStore(BASELINE_FILE)
    store.clear()

    print(f"Saved compartment regions to {ROI_FILE}")
    return chosen


def load_rois(slots: list[str], frame: np.ndarray) -> dict[str, list[int]] | None:
    """Load ROIs using ROIManager with frame shape validation."""
    manager = ROIManager(ROI_FILE)
    return manager.load(slots, frame.shape)


def crop_compartments(
    frame: np.ndarray, rois: dict[str, list[int]]
) -> dict[str, np.ndarray]:
    manager = ROIManager(ROI_FILE)
    return manager.crop(frame, rois)


def save_baseline(
    frame: np.ndarray, rois: dict[str, list[int]]
) -> dict[str, np.ndarray]:
    """Save current observation as expected baseline."""
    crops = crop_compartments(frame, rois)
    store = ReferenceStore(BASELINE_FILE)
    h, w = frame.shape[:2]
    store.save_expected(crops, {"camera_width": w, "camera_height": h})
    print("Expected baseline saved. Change a dummy object, then press V.")
    return crops


def load_baseline(slots: list[str]) -> dict[str, np.ndarray]:
    store = ReferenceStore(BASELINE_FILE)
    if store.has_expected(slots):
        return {s: store.get_expected(s) for s in slots if store.get_expected(s) is not None}
    return {}


def changed_fraction(before: np.ndarray, after: np.ndarray) -> float:
    comparator = VisualComparator()
    metrics = comparator.compare(before, after)
    return metrics.composite_diff


def log_observations(rows: list[list[Any]]) -> None:
    logger = EventLogger(LOG_FILE)
    logger.log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not logger.log_path.exists()
    import csv

    with logger.log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(logger.CSV_HEADERS)
        writer.writerows(rows)


def send_observation(server_url: str, slot: str, state: str, fraction: float) -> None:
    logger = EventLogger(LOG_FILE, server_url)
    logger.send_to_backend(slot, state, fraction, "Manual observation")


def draw_overlay(
    frame: np.ndarray,
    rois: dict[str, list[int]] | None,
    latest: dict[str, tuple[str, float]],
    message: str,
    trial_labels: dict[str, str] | None = None,
    info: str = "",
) -> np.ndarray:
    view = frame.copy()

    for slot, (x, y, w, h) in (rois or {}).items():
        state_str, fraction = latest.get(slot, ("READY", 0.0))

        if state_str in ("EXPECTED_PRESENT", "NO CHANGE"):
            color = (0, 200, 0)
        elif state_str in ("REMOVAL_DETECTED", "CHANGED"):
            color = (0, 0, 255)
        elif state_str in ("OBJECT_RETURNED",):
            color = (255, 180, 0)
        elif state_str in ("PARTIAL_OR_WRONG",):
            color = (0, 140, 255)
        elif state_str in ("UNCERTAIN", "CAMERA_ERROR"):
            color = (0, 100, 255)
        else:
            color = (0, 200, 255)

        cv2.rectangle(view, (x, y), (x + w, y + h), color, 2)
        cv2.putText(
            view,
            f"{slot}: {state_str} ({fraction:.1%})",
            (x, max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.50,
            color,
            2,
        )
        if trial_labels and slot in trial_labels:
            label = trial_labels[slot]
            cv2.putText(
                view,
                f"will: {label}",
                (x + 6, y + 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255) if label == "unchanged" else (0, 220, 255),
                2,
            )

    # Top banner
    banner_h = 100 if info else 76
    cv2.rectangle(view, (0, 0), (view.shape[1], banner_h), (20, 20, 20), -1)
    if info:
        cv2.putText(view, info[:130], (12, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (120, 230, 255), 1)
    cv2.putText(
        view,
        "MedVerify | R: regions  S: expected  E: empty  V: compare  M: monitor  T: trials  Q: quit",
        (12, 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.54,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        view,
        message[:120],
        (12, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.46,
        (180, 220, 255),
        1,
    )

    return view


def main() -> None:
    settings = load_settings()
    slots = [
        str(slot).upper()
        for slot in settings.get("compartments", ["A", "B", "C", "D"])
    ]
    server_url = str(settings.get("server_url", "http://127.0.0.1:5000"))

    camera_index = int(settings.get("camera_index", 0))
    cap = open_camera(settings, camera_index)

    if cap is None:
        print("Connect a camera or change camera_index in config/settings.json.")
        return

    ok, first_frame = cap.read()
    if not ok or first_frame is None:
        cap.release()
        print("Camera opened but no frame was available.")
        return

    roi_manager = ROIManager(ROI_FILE)
    ref_store = ReferenceStore(BASELINE_FILE)
    state_machine = CompartmentStateMachine(
        debounce_frames=int(settings.get("debounce_frames", 2)),
        state_file=STATE_FILE,
        confirm_seconds=float(settings.get("confirm_seconds", 2.0)),
    )
    monitor = ContinuousMonitor(
        slots, ref_store, state_machine, MonitorConfig.from_settings(settings)
    )
    logger = EventLogger(LOG_FILE, server_url)

    rois = roi_manager.load(slots, first_frame.shape, camera_index=camera_index)
    monitor.rois = rois
    latest: dict[str, tuple[str, float]] = {}

    if rois and ref_store.has_expected(slots):
        # Show the last persisted state. A compartment never evaluated stays UNKNOWN until the
        # camera actually looks at it: assuming "loaded" without evidence could hide an empty box.
        for s in slots:
            latest[s] = (state_machine.get_state(s).value, 0.0)

    heartbeat_every = float(settings.get("heartbeat_seconds", 5.0))
    last_heartbeat: float | None = None

    monitoring = bool(settings.get("monitor_on_start", False)) and monitor.is_ready()
    monitor_status = ""
    message = f"Camera {camera_index} | R: regions S: expected E: empty V: compare M: monitor Q: quit"

    # Trial mode: labelled experiments on a separate, non-persisted state machine.
    # Nothing from a trial reaches the real state file, the observation log, or the backend.
    recorder = TrialRecorder(DATA_DIR / "trials", slots, settings={
        k: settings.get(k) for k in (
            "change_threshold", "match_threshold", "debounce_frames", "confirm_seconds",
            "monitor_interval_seconds", "settle_seconds", "motion_threshold",
            "camera_width", "camera_height",
        )
    })
    trial_mode = False
    monitoring_before_trial = False
    trial_info = ""

    def fresh_trial_machine() -> CompartmentStateMachine:
        sm = CompartmentStateMachine(
            debounce_frames=int(settings.get("debounce_frames", 2)),
            confirm_seconds=float(settings.get("confirm_seconds", 2.0)),
        )
        for s in slots:
            sm.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
        return sm

    command_results: list[dict[str, Any]] = []
    seen_commands: set[str] = set()
    pending_recalibration: dict[str, Any] | None = None

    def calibrate_loaded(frame: np.ndarray) -> None:
        """Save the current box as the 'loaded' reference (key S, or a caretaker's recalibrate)."""
        crops = roi_manager.crop(frame, rois)
        h, w = frame.shape[:2]
        sharpness = FrameQualityAssessor().compute_blur_score(frame)
        ref_store.save_expected(
            crops,
            {"camera_index": camera_index, "camera_width": w, "camera_height": h,
             # Blur is judged relative to this: see MonitorConfig.blur_min_ratio
             "calibration_sharpness": round(sharpness, 2)},
        )
        print(f"Calibration sharpness {sharpness:.1f}; frames below "
              f"{sharpness * monitor.config.blur_min_ratio:.1f} will count as blurry.")
        for s in slots:
            state_machine.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
            latest[s] = (CompartmentState.EXPECTED_PRESENT.value, 0.0)
        monitor.reset()

    def labels_text() -> str:
        return " ".join(f"{i + 1}:{s}={recorder.labels[s]}" for i, s in enumerate(slots))

    def record(transitions: list[StateTransition], frame: np.ndarray | None = None,
               now: float | None = None, source: str = "monitor") -> int:
        """Update overlay state, print and log new events; returns the number of new events."""
        for t in transitions:
            latest[t.slot] = (t.new_state.value, float(t.metrics.get("composite_diff") or 0.0))
        new = [t for t in transitions if t.is_new_event]
        for t in new:
            print(
                f"{'[TRIAL] ' if trial_mode else ''}Compartment {t.slot}: "
                f"{t.previous_state.value} -> {t.new_state.value} ({t.reason})"
            )
        if trial_mode:
            recorder.record(frame, transitions, now if now is not None else time.monotonic(), source)
        else:
            logger.log_transitions(transitions)
        return len(new)

    def finish_trial(now: float) -> str:
        summary = recorder.finish(now, {s: monitor.state_machine.get_state(s).value for s in slots})
        lines = summary.lines()
        print("\n".join(lines))
        print(f"  saved to {summary.path}")
        return lines[0]

    def exit_trial_mode(now: float) -> None:
        nonlocal trial_mode, monitoring, trial_info
        if recorder.active:
            finish_trial(now)
        trial_mode = False
        trial_info = ""
        monitor.state_machine = state_machine
        monitor.reset()
        monitoring = monitoring_before_trial
        latest.clear()
        for s in slots:
            latest[s] = (state_machine.get_state(s).value, 0.0)

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    print("\nMedVerify camera inspector")
    print("R = define regions | C = switch camera")
    print("S = save EXPECTED baseline | E = save EMPTY baseline")
    print("V = compare once | M = start/stop continuous monitoring | Q = quit")
    print("T = trial mode: 1-4 label compartments, N start trial, F finish trial")

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                message = "Frame read failed. Press C to try another camera."
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                continue

            now = time.monotonic()
            if last_heartbeat is None or now - last_heartbeat >= heartbeat_every:
                # Liveness + state snapshot: lets the backend notice a dead camera and
                # recover any observation whose POST was lost.
                last_heartbeat = now
                # Results are repeated for 30 s: a heartbeat may be lost, and the server
                # ignores results for commands it has already closed.
                command_results[:] = [r for r in command_results if now - r["_at"] < 30.0]
                logger.send_heartbeat({
                    # During trials the real doses are NOT being watched: say so, send no states.
                    "monitoring": monitoring and not trial_mode,
                    "trial": trial_mode,
                    "camera_index": camera_index,
                    "states": {} if trial_mode else {s: state_machine.get_state(s).value for s in slots},
                    "command_results": [{k: v for k, v in r.items() if k != "_at"} for r in command_results],
                })

            for cmd in logger.take_commands():
                if cmd["id"] not in seen_commands and cmd.get("type") == "recalibrate":
                    seen_commands.add(cmd["id"])
                    pending_recalibration = {"id": cmd["id"], "since": now}
                    message = "Caretaker asked to recalibrate: keep hands out of the box."
                    print(message)

            tick = None
            if monitoring:
                tick = monitor.process(frame, now)
                q = monitor.last_quality
                sharp = (f" sharp {q.blur_score:.0f}/{monitor.blur_limit():.0f}"
                         f"{' DARK' if q.is_underexposed else ''}" if q is not None else "")
                monitor_status = (f"[{'TRIAL' if trial_mode else 'MONITOR'} {tick.status} "
                                  f"motion {tick.motion:.1%}{sharp}] ")
                if tick.transitions and record(tick.transitions, frame, now):
                    message = ", ".join(
                        f"{t.slot}: {t.new_state.value}" for t in tick.new_events
                    )
            else:
                monitor_status = ""

            if pending_recalibration is not None:
                outcome = None
                if trial_mode:
                    outcome = (False, "The camera is in trial mode. Leave it (T) and try again.")
                elif not rois:
                    outcome = (False, "Compartment regions are not set on the camera (press R there).")
                elif (tick is not None and tick.status in (WAITING, EVALUATED)) or (
                        tick is None and now - pending_recalibration["since"] >= 2.0):
                    # Box has been still for the settle period (or monitoring is off): photograph it.
                    calibrate_loaded(frame)
                    outcome = (True, "New 'loaded' photos saved.")
                elif now - pending_recalibration["since"] > 60.0:
                    outcome = (False, "The box never stayed still long enough. Keep hands away and retry.")
                if outcome is not None:
                    command_results.append({"id": pending_recalibration["id"], "ok": outcome[0],
                                            "message": outcome[1], "_at": now})
                    pending_recalibration = None
                    last_heartbeat = None  # Report the result on the next frame
                    message = f"Recalibration {'done' if outcome[0] else 'failed'}: {outcome[1]}"
                    print(message)

            if trial_mode:
                if not recorder.active:
                    trial_info = f"TRIAL MODE | {labels_text()} | 1-4: label  N: start  T: exit"
                elif not recorder.ready:
                    trial_info = "TRIAL: hands off, checking the box matches calibration..."
                elif recorder.start_mismatch_slots:
                    trial_info = (f"TRIAL: {', '.join(recorder.start_mismatch_slots)} does not match calibration. "
                                  "Fix the box and press N to restart.")
                else:
                    trial_info = (f"TRIAL: GO - do it now. {recorder.elapsed(now):.0f}s, "
                                  f"{recorder.evaluations} checks. Wait for states to settle, then F")
            view = draw_overlay(frame, rois, latest, monitor_status + message,
                                trial_labels=recorder.labels if trial_mode else None, info=trial_info)
            cv2.imshow(WINDOW, view)
            raw_key = cv2.waitKey(1) & 0xFF
            key = chr(raw_key).lower() if raw_key != 255 else ""

            if trial_mode and key in ("r", "c"):
                # Regions or camera are about to change: trials must not span that.
                exit_trial_mode(now)
                print("Left trial mode.")

            if key == "q":
                break

            elif key == "t":
                if trial_mode:
                    exit_trial_mode(now)
                    message = "Left trial mode; real monitoring state restored."
                elif not monitor.is_ready():
                    message = "Trials need regions (R) and an expected baseline (S); an empty one (E) too."
                else:
                    trial_mode = True
                    monitoring_before_trial = monitoring
                    monitoring = True
                    monitor.state_machine = fresh_trial_machine()
                    monitor.reset()
                    latest.clear()
                    for s in slots:
                        latest[s] = (CompartmentState.EXPECTED_PRESENT.value, 0.0)
                    message = "Trial mode: set labels with 1-4, put the box as calibrated, press N."
                    if not ref_store.has_empty(slots):
                        message += " No empty reference yet: press E with the box empty first."
                print(message)

            elif trial_mode and key in tuple(str(i + 1) for i in range(len(slots))):
                if recorder.active:
                    message = "Labels are locked while a trial runs. Press F first."
                else:
                    slot = slots[int(key) - 1]
                    message = f"{slot} will be {recorder.cycle_label(slot)}."

            elif trial_mode and key == "n":
                if recorder.active:
                    message = finish_trial(now)
                monitor.state_machine = fresh_trial_machine()
                monitor.reset()
                latest.clear()
                for s in slots:
                    latest[s] = (CompartmentState.EXPECTED_PRESENT.value, 0.0)
                trial_id = recorder.start(now, rois, ref_store, camera_index)
                message = "Trial started. Keep hands away until the banner says GO."
                print(f"[TRIAL] started {trial_id}: {labels_text()}")

            elif trial_mode and key == "f":
                message = finish_trial(now) if recorder.active else "No trial running. Press N to start one."

            elif key == "m" and trial_mode:
                message = "Monitoring is always on in trial mode. Press T to leave."

            elif key == "m":
                if monitoring:
                    monitoring = False
                    message = "Continuous monitoring stopped."
                elif not monitor.is_ready():
                    message = "Monitoring needs regions (R) and an expected baseline (S)."
                else:
                    monitor.reset()
                    monitoring = True
                    message = "Continuous monitoring started."
                print(message)

            elif key == "c":
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
                monitor.rois = None
                monitor.reset()
                monitoring = False
                ref_store.clear()
                state_machine.clear_state()
                latest = {}
                message = (
                    f"Switched to camera {camera_index}. "
                    "Press R to redraw compartment regions."
                )
                print(f"Switched from camera {old_index} to {camera_index}.")
                continue

            elif key == "r":
                ok, f = cap.read()
                if not ok or f is None:
                    message = "Cannot select regions: frame unavailable."
                    continue

                new_rois = select_rois(
                    f.copy(), slots, ref_store=ref_store, camera_index=camera_index
                )
                if new_rois is not None:
                    rois = new_rois
                    monitor.rois = rois
                    monitor.reset()
                    monitoring = False
                    state_machine.clear_state()
                    latest = {}
                    message = "Regions saved; references cleared. Arrange expected medication, then press S."
                else:
                    message = "Selection cancelled; live feed continues."

            elif key == "s":
                if not rois:
                    message = "Press R to define all compartment regions first."
                    continue

                calibrate_loaded(frame)
                message = "Expected baseline saved. Press M to monitor, or V to compare once."
                print("Saved expected baseline.")

            elif key == "e":
                if not rois:
                    message = "Press R to define all compartment regions first."
                    continue

                crops = roi_manager.crop(frame, rois)
                h, w = frame.shape[:2]
                ref_store.save_empty(
                    crops,
                    {"camera_index": camera_index, "camera_width": w, "camera_height": h},
                )
                monitor.reset()
                message = "Empty compartment baseline saved."
                print("Saved empty compartment baseline.")

            elif key == "v":
                if not rois:
                    message = "Press R to define compartment regions first."
                    continue

                if not ref_store.has_expected(slots):
                    message = "No expected baseline. Arrange medication and press S."
                    continue

                # Manual compare is an explicit request, so it bypasses the stability gate.
                transitions = monitor.evaluate(frame)
                new_events = record(transitions, frame, time.monotonic(), "manual")
                changed_count = sum(
                    1 for t in transitions if t.new_state != CompartmentState.EXPECTED_PRESENT
                )

                # CRITICAL: Do NOT overwrite ref_store.expected!
                # Baseline is preserved immutably.
                message = (
                    f"Compared {len(slots)} compartments; "
                    f"{changed_count} changed, {new_events} new event(s) logged. "
                    "(Expected baseline preserved)"
                )

    finally:
        if recorder.active:
            finish_trial(time.monotonic())  # Keep the data of a trial interrupted by Q
        cap.release()
        cv2.destroyAllWindows()
        # Give queued backend deliveries a bounded chance to finish; the CSV already has them.
        if not logger.close(timeout=2.0):
            print("Some observations were not delivered to the backend before exit (kept in local CSV).")
        if logger.failed_count or logger.dropped_count:
            print(
                f"Backend delivery: {logger.delivered_count} sent, "
                f"{logger.failed_count} failed, {logger.dropped_count} dropped. "
                f"Last error: {logger.last_delivery_error}"
            )

if __name__ == "__main__":
    main()
