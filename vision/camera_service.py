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
from vision.drawn_regions import find_drawn_cells, find_drawn_layout, line_mask
from vision.event_logger import EventLogger
from vision.frame_quality import FrameQualityAssessor
from vision.monitor import EVALUATED, WAITING, ContinuousMonitor, MonitorConfig
from vision.reference_store import ReferenceStore
from vision.roi_manager import ROIManager
from vision.state_machine import CompartmentState, CompartmentStateMachine
from vision.trials import LABELS, TrialRecorder

ROOT = Path(__file__).resolve().parents[1]
MAX_SHEET_SHIFT = 40  # px; a bigger move means the camera or sheet really moved: start over


def _sheet_shift(then: np.ndarray, now: np.ndarray) -> tuple[int, int]:
    """(dx, dy) that moves `then` onto `now`, measured on the pen lines (medicine differs between
    the two photos by design, the drawing does not). (0, 0) when there is no drawing to follow."""
    a, b = line_mask(then).astype(np.float32), line_mask(now).astype(np.float32)
    if np.count_nonzero(a) < 500 or np.count_nonzero(b) < 500:
        return 0, 0
    (dx, dy), response = cv2.phaseCorrelate(a, b)
    if response < 0.05:
        return 0, 0
    return int(round(dx)), int(round(dy))


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
    @property
    def empty_frame_file(self) -> Path: return self.data_dir / "vision_empty_frame.png"


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
        self.monitor.set_outlines(self.roi_manager.load_outlines(self.slots) if self.rois else {})
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
                "view_blocked": sorted(self.monitor.last_blocked),
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

    def _pieces_on_edges(self, frame: np.ndarray) -> list[str]:
        """Compartments where something crosses the border (the drawn line, or the box edge for
        plain regions), judged against the empty photo. A piece half outside is only partly
        counted, so its removal could never be confirmed: refuse such a calibration instead."""
        crops = self.roi_manager.crop(frame, self.rois)
        bad = []
        for slot in self.slots:
            empty = self.ref_store.get_empty(slot)
            if empty is not None and self.monitor.view_blocked(slot, crops[slot], empty):
                bad.append(slot)
        return bad

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
                # Keep the whole empty sheet (lossless) so regions can be redrawn later without
                # clearing the sheet again, as long as the camera has not moved.
                cv2.imwrite(str(self.paths.empty_frame_file), frame)
                self._result(cmd, True, "Saved the 'empty' photos.", now)
            elif self._pieces_on_edges(frame):
                slots = ", ".join(self._pieces_on_edges(frame))
                where = "lies across the drawn line" if self.monitor.inside else "touches the edge of its box"
                self._result(cmd, False, f"Something in {slots} {where}: a hand in view, or a piece half "
                                         "outside. Keep hands away, put pieces inside (see `mv cam snapshot`), "
                                         "and retry.", now)
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
        view = draw_regions(frame, self.rois or {}, states, self.roi_manager.load_outlines(self.slots))
        cv2.imwrite(str(annotated), view, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        self._result(cmd, True, f"Saved {annotated}", now, {"raw": str(raw), "annotated": str(annotated)})

    def _set_regions(self, cmd, params, frame, now) -> None:
        if params.get("preview"):
            # Show what auto-detection sees, numbered, and change nothing (for placing the camera
            # and for choosing which number is which compartment letter). Pen-drawn squares win.
            cells = find_drawn_cells(frame)
            if cells:
                self._preview_drawn(cmd, frame, cells, now)
                return
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
            if params.get("auto"):
                rois, outlines = self._auto_regions(frame, params.get("assign"))
            else:
                rois, outlines = {str(k).upper(): [int(v) for v in box]
                                  for k, box in (params.get("rois") or {}).items()}, {}
        except (RegionsNotFound, TypeError, ValueError) as exc:
            self._result(cmd, False, str(exc), now)
            return
        self.roi_manager.save(rois, frame.shape, camera_index=self.camera_index, outlines=outlines)
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
        self.monitor.set_outlines(outlines)
        self.ref_store.clear()
        self.state_machine.clear_state()
        self.monitoring = False
        self.monitor.reset()
        if self._reuse_empty_frame(frame, checked):
            self._result(cmd, True, "Regions saved; the empty photo was re-cut from the saved empty sheet "
                                    "(camera has not moved). Next: `mv cam loaded A=.. B=..`.", now,
                         {"rois": checked, "empty_reused": True})
            return
        self._result(cmd, True, "Regions saved. Next: empty the sheet and `mv cam empty`, then load it and "
                                "`mv cam loaded`.", now, {"rois": checked, "empty_reused": False})

    def _auto_regions(self, frame: np.ndarray, assign: dict | None
                      ) -> tuple[dict[str, list[int]], dict[str, list[list[int]]]]:
        """Pen-drawn squares (with their exact outlines) when the sheet has any; otherwise boxes
        around the medicine groups."""
        if find_drawn_cells(frame):
            return find_drawn_layout(frame, self.slots, assign=assign)
        return find_regions(frame, self.slots, assign=assign), {}

    def _preview_drawn(self, cmd, frame: np.ndarray, cells: list, now: float) -> None:
        listing = "\n".join(f"  {i + 1}: {c.contents:<7} at x={c.box[0]} y={c.box[1]} w={c.box[2]} h={c.box[3]}"
                            for i, c in enumerate(cells))
        self.paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.snapshots_dir / "regions_preview.jpg"
        labelled = {f"{i + 1} {c.contents}": c.box for i, c in enumerate(cells)}
        shapes = {f"{i + 1} {c.contents}": c.outline for i, c in enumerate(cells)}
        cv2.imwrite(str(path), draw_regions(frame, labelled, outlines=shapes), [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        enough = len(cells) >= len(self.slots)
        verdict = (f"{len(cells)} complete drawn square(s):\n" + listing
                   + ("" if enough else f"\nNeed {len(self.slots)}: every square must be fully in view, "
                                        "with its pen lines closed."))
        self._result(cmd, True, f"{verdict}\nPreview image: {path}", now,
                     {"ok": enough, "drawn": True,
                      "squares": [{"number": i + 1, "contents": c.contents, "fill": c.fill, "box": c.box}
                                  for i, c in enumerate(cells)],
                      "image": str(path)})

    def _reuse_empty_frame(self, frame: np.ndarray, rois: dict[str, list[int]]) -> bool:
        """Re-cut the empty references for new regions from the saved empty sheet, if the camera
        still sees the same scene. Judged on the paper OUTSIDE the regions (inside, the medicine
        differs by design): if the camera moved, that paper no longer lines up."""
        path = self.paths.empty_frame_file
        if not path.exists():
            return False
        empty = cv2.imread(str(path))
        if empty is None or empty.shape != frame.shape:
            return False
        # A sheet that slid a little while pieces were handled (measured: 10 px) is followed by
        # shifting the empty photo onto the drawn grid's new position.
        dx, dy = _sheet_shift(empty, frame)
        if (dx or dy) and max(abs(dx), abs(dy)) <= MAX_SHEET_SHIFT:
            empty = cv2.warpAffine(empty, np.float32([[1, 0, dx], [0, 1, dy]]), (frame.shape[1], frame.shape[0]),
                                   borderMode=cv2.BORDER_REPLICATE)
            print(f"The sheet moved by ({dx}, {dy}) px since the empty photo: shifted it to match.")
        outside = np.ones(frame.shape[:2], dtype=bool)
        for x, y, w, h in rois.values():
            outside[y:y + h, x:x + w] = False
        g_now = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
        g_then = cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY).astype(np.int16)
        # Even out overall brightness, then compare the paper pixel by pixel.
        g_now = g_now - int(np.median(g_now[outside])) + int(np.median(g_then[outside]))
        moved = float(np.mean(np.abs(g_now - g_then)[outside] > 25))
        if moved > 0.05:
            path.unlink(missing_ok=True)
            print(f"Camera view changed since the empty photo ({moved:.0%} of the paper differs): not reused.")
            return False
        sharp = self.monitor.assessor.compute_blur_score(empty)
        self.ref_store.save_empty(self.roi_manager.crop(empty, rois), {"camera_index": self.camera_index,
                                                                       "empty_sharpness": round(sharp, 2)})
        return True

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
