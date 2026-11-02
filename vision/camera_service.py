"""Headless camera service: the full camera pipeline with no window, driven by commands.

    .venv\\Scripts\\python.exe -m vision.camera_service      (normally started by `mv up`)

It does everything the camera window does (monitoring, calibration, trials), but every
action arrives as a command from the server (sent with `mv cam ...` or from the apps),
so the whole system can be run from a terminal or VS Code. It sends a heartbeat every
second with its state, and picks up commands from the heartbeat replies.

Commands (see backend/commands.py): set_regions, save_empty, recalibrate (= save loaded),
monitor, snapshot, trial_start, trial_finish. Calibration commands wait until the scene
has been still for `settle_seconds`, so a hand in view is never photographed as "loaded".

Snapshots are taken only when a `snapshot` command asks for one.
"""
from __future__ import annotations

import json
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from vision.auto_regions import RegionsNotFound, detect_groups, draw_regions, find_regions
from vision.event_logger import EventLogger
from vision.frame_quality import FrameQualityAssessor
from vision.monitor import EVALUATED, WAITING, ContinuousMonitor, MonitorConfig
from vision.reference_store import ReferenceStore
from vision.roi_manager import ROIManager
from vision.state_machine import CompartmentState, CompartmentStateMachine
from vision.trials import LABELS, TrialRecorder

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ServicePaths:
    config_dir: Path
    data_dir: Path

    @property
    def roi_file(self) -> Path: return self.config_dir / "rois.json"
    @property
    def baseline_file(self) -> Path: return self.data_dir / "vision_baseline.npz"
    @property
    def state_file(self) -> Path: return self.data_dir / "vision_state.json"
    @property
    def log_file(self) -> Path: return self.data_dir / "vision_observations.csv"
    @property
    def trials_dir(self) -> Path: return self.data_dir / "trials"
    @property
    def snapshots_dir(self) -> Path: return self.data_dir / "snapshots"


class StillnessTracker:
    """How long the watched area has been still (independent of monitoring being on)."""

    def __init__(self, assessor: FrameQualityAssessor) -> None:
        self.assessor = assessor
        self._prev: np.ndarray | None = None
        self._still_since: float | None = None

    def update(self, frame: np.ndarray, now: float) -> float:
        small = cv2.resize(frame, (160, 120))
        if self._prev is None or self._prev.shape != small.shape:
            self._prev, self._still_since = small, None
            return 0.0
        stable, _ = self.assessor.check_motion_stability([self._prev, small])
        self._prev = small
        if not stable:
            self._still_since = None
            return 0.0
        if self._still_since is None:
            self._still_since = now
        return now - self._still_since


class CameraService:
    def __init__(self, settings: dict[str, Any], paths: ServicePaths, logger: EventLogger,
                 camera_index: int = 0) -> None:
        self.settings = settings
        self.paths = paths
        self.logger = logger
        self.camera_index = camera_index
        self.slots = [str(s).upper() for s in settings.get("compartments", ["A", "B", "C", "D"])]
        self.config = MonitorConfig.from_settings(settings)
        self.heartbeat_every = float(settings.get("service_heartbeat_seconds", 1.0))
        self.still_needed = float(settings.get("settle_seconds", 1.0))

        self.roi_manager = ROIManager(paths.roi_file)
        self.ref_store = ReferenceStore(paths.baseline_file)
        self.state_machine = CompartmentStateMachine(
            debounce_frames=int(settings.get("debounce_frames", 2)),
            state_file=paths.state_file,
            confirm_seconds=float(settings.get("confirm_seconds", 2.0)),
        )
        self.monitor = ContinuousMonitor(self.slots, self.ref_store, self.state_machine, self.config)
        self.stillness = StillnessTracker(FrameQualityAssessor(motion_threshold=self.config.motion_threshold))
        self.recorder = TrialRecorder(paths.trials_dir, self.slots, settings={
            k: settings.get(k) for k in ("change_threshold", "match_threshold", "debounce_frames", "confirm_seconds",
                                         "monitor_interval_seconds", "settle_seconds", "motion_threshold",
                                         "blur_min_ratio")})

        self.rois: dict[str, list[int]] | None = None
        self.frame_size: tuple[int, int] | None = None
        self.monitoring = False
        self.trial_mode = False
        self._monitoring_before_trial = False
        self.last_tick_status = ""
        self.last_frame: np.ndarray | None = None
        self.last_still_s = 0.0
        self._last_heartbeat: float | None = None
        self._results: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        self._waiting: dict[str, Any] | None = None  # a calibration waiting for a still scene

    # ---------------------------------------------------------------- set-up
    def attach(self, first_frame: np.ndarray) -> None:
        """Called with the first frame: load regions saved for this camera and resolution."""
        self.frame_size = (first_frame.shape[1], first_frame.shape[0])
        self.rois = self.roi_manager.load(self.slots, first_frame.shape, camera_index=self.camera_index)
        self.monitor.rois = self.rois
        self.monitoring = bool(self.settings.get("monitor_on_start", True)) and self.monitor.is_ready()
        if self.rois is None:
            print("No compartment regions saved for this camera/resolution yet: send `mv cam regions auto`.")

    # ------------------------------------------------------------------ loop
    def step(self, frame: np.ndarray, now: float) -> None:
        self.last_frame = frame
        self.last_still_s = self.stillness.update(frame, now)
        for cmd in self.logger.take_commands():
            if cmd["id"] not in self._seen:
                self._seen.add(cmd["id"])
                self._handle(cmd, frame, now)

        tick = None
        if self.monitoring:
            tick = self.monitor.process(frame, now)
            self.last_tick_status = tick.status
            if tick.transitions:
                for t in tick.new_events:
                    print(f"{'[TRIAL] ' if self.trial_mode else ''}{t.slot}: {t.previous_state.value} -> "
                          f"{t.new_state.value} ({t.reason})")
                if self.trial_mode:
                    self.recorder.record(frame, tick.transitions, now)
                else:
                    self.logger.log_transitions(tick.transitions)
            for slot, old, new in tick.count_changes:
                print(f"{'[TRIAL] ' if self.trial_mode else ''}{slot}: count {old} -> {new}")
                if not self.trial_mode:
                    estimate = self.monitor.counters[slot].last_estimate
                    self.logger.log_count_change(slot, old, new, estimate,
                                                 time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        else:
            self.last_tick_status = "OFF"

        if self._waiting is not None:
            self._run_waiting(frame, now)

        if self._last_heartbeat is None or now - self._last_heartbeat >= self.heartbeat_every:
            self._last_heartbeat = now
            self._results = [r for r in self._results if now - r["_at"] < 30.0]
            self.logger.send_heartbeat(self.heartbeat_payload(now))

    def heartbeat_payload(self, now: float) -> dict[str, Any]:
        sm = self.monitor.state_machine
        # Measured on the current frame every heartbeat, so it is live even with monitoring off
        # (that is when the camera is being positioned).
        q = self.monitor.assessor.assess(self.last_frame) if self.last_frame is not None else None
        return {
            "monitoring": self.monitoring and not self.trial_mode,
            "trial": self.trial_mode,
            "camera_index": self.camera_index,
            "states": {} if self.trial_mode else {s: self.state_machine.get_state(s).value for s in self.slots},
            "command_results": [{k: v for k, v in r.items() if k != "_at"} for r in self._results],
            # Confirmed pill counts (compartments calibrated with a count). Not sent during trials:
            # trial counts are experiments, not the real box.
            "counts": {} if self.trial_mode else {k: v for k, v in self.monitor.counts().items() if v is not None},
            "loaded_counts": self.monitor.loaded_counts(),
            "details": {
                "service": "headless",
                "frame_size": list(self.frame_size) if self.frame_size else None,
                "regions": self.rois,
                "has_loaded_photo": self.ref_store.has_expected(self.slots),
                "has_empty_photo": self.ref_store.has_empty(self.slots),
                "monitor_status": self.last_tick_status,
                "still_seconds": round(self.last_still_s, 1),
                "sharpness": round(q.blur_score, 1) if q is not None else None,
                "sharpness_limit": round(self.monitor.blur_limit(), 1),
                "live_states": {s: sm.get_state(s).value for s in self.slots},
                "count_estimates": {slot: (round(c.last_estimate, 2) if c.last_estimate is not None else None)
                                    for slot, c in self.monitor.counters.items()},
                "evidence": "area" if self.monitor.uses_content() else "pixels",
                "waiting_for": self._waiting["type"] if self._waiting else None,
                "trial": {
                    "active": self.recorder.active,
                    "ready": self.recorder.ready,
                    "elapsed": round(self.recorder.elapsed(now), 1) if self.recorder.active else 0,
                    "labels": dict(self.recorder.labels),
                    "start_mismatch": list(self.recorder.start_mismatch_slots),
                } if self.trial_mode else None,
            },
        }

    # -------------------------------------------------------------- commands
    def _result(self, cmd: dict[str, Any], ok: bool, message: str, now: float, data: Any = None) -> None:
        self._results.append({"id": cmd["id"], "ok": ok, "message": message, "data": data, "_at": now})
        self._last_heartbeat = None  # report on the next step
        print(f"[{cmd['type']}] {'done' if ok else 'failed'}: {message}")

    def _handle(self, cmd: dict[str, Any], frame: np.ndarray, now: float) -> None:
        kind, params = cmd.get("type"), cmd.get("params") or {}
        handler: Callable[..., None] | None = {
            "snapshot": self._snapshot, "set_regions": self._set_regions, "monitor": self._monitor,
            "trial_start": self._trial_start, "trial_finish": self._trial_finish,
        }.get(kind)
        if handler is not None:
            handler(cmd, params, frame, now)
        elif kind in ("recalibrate", "save_empty"):
            if not self.rois:
                self._result(cmd, False, "No compartment regions yet: run `mv cam regions auto` first.", now)
            elif self.trial_mode:
                self._result(cmd, False, "Finish the trial first (`mv trial finish`).", now)
            elif not self._counts_valid(params.get("counts")):
                self._result(cmd, False, f"Counts must be whole numbers 1-50 for compartments {self.slots}.", now)
            else:
                self._waiting = {**cmd, "since": now}
                print(f"[{kind}] waiting for the scene to be still for {self.still_needed:.0f} s...")
        else:
            self._result(cmd, False, f"Unknown command {kind}", now)

    def _counts_valid(self, counts: Any) -> bool:
        if counts is None:
            return True
        if not isinstance(counts, dict):
            return False
        try:
            return all(str(k).upper() in self.slots and 1 <= int(v) <= 50 for k, v in counts.items())
        except (TypeError, ValueError):
            return False

    def _run_waiting(self, frame: np.ndarray, now: float) -> None:
        cmd = self._waiting
        if self.last_still_s >= self.still_needed:
            quality = self.monitor.assessor.assess(frame)
            if quality.is_underexposed or quality.is_overexposed:
                self._result(cmd, False, f"Image too dark or bright ({quality.reason}). Fix the light and retry.", now)
            elif cmd["type"] == "save_empty":
                crops = self.roi_manager.crop(frame, self.rois)
                self.ref_store.save_empty(crops, {"camera_index": self.camera_index,
                                                  "empty_sharpness": round(quality.blur_score, 2)})
                self._result(cmd, True, "Saved the 'empty' photos.", now)
            else:
                counts = cmd.get("params", {}).get("counts")
                self.calibrate_loaded(frame, counts)
                self.monitoring = True
                self.monitor.reset()
                declared = self.monitor.loaded_counts()
                note = (" Pill counts: " + ", ".join(f"{k}={v}" for k, v in sorted(declared.items()))
                        + ". One pill = " + ", ".join(
                            f"{k} {self.monitor.reference_stats()[k]['unit']:.0f} px" for k in sorted(declared))
                        if declared else
                        " No pill counts given: removals are judged as 'emptied', not counted.")
                self._result(cmd, True, "Saved the 'loaded' photos; monitoring is on." + note, now,
                             {"loaded_counts": declared})
            self._waiting = None
        elif now - cmd["since"] > 60.0:
            self._result(cmd, False, "The scene never stayed still for long enough. Keep hands away and retry.", now)
            self._waiting = None

    def calibrate_loaded(self, frame: np.ndarray, counts: dict[str, Any] | None = None) -> None:
        crops = self.roi_manager.crop(frame, self.rois)
        sharpness = self.monitor.assessor.compute_blur_score(frame)
        h, w = frame.shape[:2]
        meta: dict[str, Any] = {"camera_index": self.camera_index, "camera_width": w, "camera_height": h,
                                "calibration_sharpness": round(sharpness, 2)}
        if counts:
            # Counts declared now replace earlier ones; a refill without counts keeps the old ones.
            merged = dict(self.ref_store.metadata.get("loaded_counts") or {})
            merged.update({str(k).upper(): int(v) for k, v in counts.items() if int(v) >= 1})
            meta["loaded_counts"] = merged
        self.ref_store.save_expected(crops, meta)
        for s in self.slots:
            self.state_machine.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)

    def _snapshot(self, cmd, params, frame, now) -> None:
        d = self.paths.snapshots_dir
        d.mkdir(parents=True, exist_ok=True)
        raw, annotated = d / "latest.jpg", d / "latest_annotated.jpg"
        cv2.imwrite(str(raw), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        states = {s: self.monitor.state_machine.get_state(s).value for s in self.slots}
        cv2.imwrite(str(annotated), draw_regions(frame, self.rois or {}, states), [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        self._result(cmd, True, f"Saved {annotated}", now, {"raw": str(raw), "annotated": str(annotated)})

    def _set_regions(self, cmd, params, frame, now) -> None:
        if params.get("preview"):
            # Show what auto-detection sees, numbered, and change nothing (for placing the camera
            # and for choosing which group number is which compartment letter).
            groups, ignored = detect_groups(frame)
            listing = "\n".join(f"  {i + 1}: {g.color:<7} at x={g.box[0]} y={g.box[1]} w={g.box[2]} h={g.box[3]}"
                                for i, g in enumerate(groups))
            self.paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.paths.snapshots_dir / "regions_preview.jpg"
            labelled = {f"{i + 1} {g.color}": g.box for i, g in enumerate(groups)}
            cv2.imwrite(str(path), draw_regions(frame, labelled), [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            enough = len(groups) >= len(self.slots)
            verdict = (f"{len(groups)} group(s) on the sheet"
                       + (f", {ignored} object(s) at the picture edge ignored" if ignored else "") + ":\n" + listing
                       + ("" if enough else f"\nNeed {len(self.slots)}: every group must be fully in view."))
            self._result(cmd, True, f"{verdict}\nPreview image: {path}", now,
                         {"ok": enough, "groups": [{"number": i + 1, "color": g.color, "box": g.box}
                                                   for i, g in enumerate(groups)],
                          "ignored": ignored, "image": str(path)})
            return
        try:
            rois = find_regions(frame, self.slots, assign=params.get("assign")) if params.get("auto") else {
                str(k).upper(): [int(v) for v in box] for k, box in (params.get("rois") or {}).items()}
        except (RegionsNotFound, TypeError, ValueError) as exc:
            self._result(cmd, False, str(exc), now)
            return
        self.roi_manager.save(rois, frame.shape, camera_index=self.camera_index)
        checked = self.roi_manager.load(self.slots, frame.shape, camera_index=self.camera_index)
        if checked is None:
            self._result(cmd, False, "Regions rejected: every compartment needs a box of at least 15x15 px "
                                     "inside the image.", now)
            return
        if self.trial_mode:
            self._end_trial(now)
        # New layout: old photos and states no longer describe it (same rule as the R key).
        self.rois = checked
        self.monitor.rois = checked
        self.ref_store.clear()
        self.state_machine.clear_state()
        self.monitoring = False
        self.monitor.reset()
        self._result(cmd, True, "Regions saved. Next: empty the sheet and `mv cam empty`, then load it and "
                                "`mv cam loaded`.", now, {"rois": checked})

    def _monitor(self, cmd, params, frame, now) -> None:
        on = bool(params.get("on", True))
        if on and not self.monitor.is_ready():
            self._result(cmd, False, "Needs regions and a 'loaded' photo first.", now)
            return
        self.monitoring = on
        self.monitor.reset()
        self._result(cmd, True, f"Monitoring {'on' if on else 'off'}.", now)

    def _trial_start(self, cmd, params, frame, now) -> None:
        labels = {str(k).upper(): str(v).lower() for k, v in (params.get("labels") or {}).items()}
        bad = [f"{k}={v}" for k, v in labels.items() if k not in self.slots or v not in LABELS]
        if bad:
            self._result(cmd, False, f"Bad labels {', '.join(bad)}. Use compartments {self.slots} and "
                                     f"one of {list(LABELS)}.", now)
            return
        if not self.monitor.is_ready():
            self._result(cmd, False, "Needs regions and a 'loaded' photo first.", now)
            return
        if self.recorder.active:
            self.recorder.finish(now, {s: self.monitor.state_machine.get_state(s).value for s in self.slots})
        if not self.trial_mode:
            self._monitoring_before_trial = self.monitoring
            self._counts_before_trial = self.monitor.counts()
        self.trial_mode = True
        self.monitoring = True
        sm = CompartmentStateMachine(debounce_frames=int(self.settings.get("debounce_frames", 2)),
                                     confirm_seconds=float(self.settings.get("confirm_seconds", 2.0)))
        for s in self.slots:
            sm.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
            self.recorder.labels[s] = labels.get(s, "unchanged")
        self.monitor.state_machine = sm
        self.monitor.reset()
        trial_id = self.recorder.start(now, self.rois, self.ref_store, self.camera_index)
        self._result(cmd, True, f"Trial {trial_id} started. Hands off until it says GO.", now, {"trial_id": trial_id})

    def _trial_finish(self, cmd, params, frame, now) -> None:
        if not self.recorder.active:
            self._result(cmd, False, "No trial is running.", now)
            return
        trial_counts = {k: v for k, v in self.monitor.counts().items() if v is not None}
        summary = self._end_trial(now)
        lines = summary.lines() + ([f"  counts at the end of the trial: {trial_counts}"] if trial_counts else [])
        self._result(cmd, True, "\n".join(lines), now,
                     {"trial_id": summary.trial_id, "passed": summary.passed, "total": len(summary.results),
                      "flags": summary.flags, "path": str(summary.path), "counts": trial_counts})

    def _end_trial(self, now: float):
        summary = None
        if self.recorder.active:
            summary = self.recorder.finish(now, {s: self.monitor.state_machine.get_state(s).value for s in self.slots})
        self.trial_mode = False
        self.monitor.state_machine = self.state_machine
        for slot, count in getattr(self, "_counts_before_trial", {}).items():
            if slot in self.monitor.counters:
                self.monitor.counters[slot].reset(count)
        self.monitor.reset()
        self.monitoring = self._monitoring_before_trial and self.monitor.is_ready()
        return summary


def load_settings() -> dict[str, Any]:
    try:
        return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    from vision.camera_inspector import open_camera  # camera opening + exposure warm-up

    settings = load_settings()
    paths = ServicePaths(ROOT / "config", ROOT / "data")
    index = int(settings.get("camera_index", 0))
    logger = EventLogger(paths.log_file, str(settings.get("server_url", "http://127.0.0.1:5000")))
    service = CameraService(settings, paths, logger, camera_index=index)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    cap = open_camera(settings, index)
    if cap is None:
        print(f"Camera {index} could not be opened. Is another program (Camera app, Teams, the camera window) using it?")
        raise SystemExit(1)
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            print("Camera opened but gave no image.")
            raise SystemExit(1)
        service.attach(frame)
        print(f"Camera service running: camera {index}, {frame.shape[1]}x{frame.shape[0]}, "
              f"monitoring {'on' if service.monitoring else 'off'}.")
        failures = 0
        while not stop.is_set():
            if cap is None:  # Unplugged camera: keep trying, the heartbeat stops meanwhile
                time.sleep(2.0)
                cap = open_camera(settings, index)
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 30:  # about 1-3 s of failed reads: try to reopen the camera
                    print("Camera stopped delivering images; reopening...")
                    cap.release()
                    time.sleep(1.0)
                    cap = open_camera(settings, index)
                    failures = 0
                continue
            failures = 0
            service.step(frame, time.monotonic())
    finally:
        if service.recorder.active:
            service._end_trial(time.monotonic())
        if cap is not None:
            cap.release()
        logger.close(timeout=2.0)
        print("Camera service stopped.")


if __name__ == "__main__":
    main()
