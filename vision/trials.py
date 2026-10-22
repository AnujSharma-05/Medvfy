"""Labelled trials for measuring (and later tuning) the camera pipeline.

A trial is one controlled experiment: the box starts as calibrated ("loaded"), the
tester says in advance what they will do to each compartment, does it, and finishes
the trial. Everything the camera measured in between is saved so the decision can be
graded now and replayed later under different thresholds (tools/evaluate_trials.py).

Labels (what the tester will do to a compartment):
    unchanged   leave it alone                      -> expect EXPECTED_PRESENT / OBJECT_RETURNED
    removed     take everything out                 -> expect REMOVAL_DETECTED / EMPTY
    changed     swap, add a foreign object, or take only part -> expect PARTIAL_OR_WRONG

Layout on disk (data/trials/<trial_id>/):
    trial.json         labels, regions, settings snapshot, timing, graded results
    references.npz     the calibration crops in force for this trial (expected_X / empty_X)
    evaluations.jsonl  one line per evaluation: time, frame file, per-compartment scores + state
    frames/0001.jpg    the full camera frame for each evaluation (JPEG quality 95)

Trials never touch the real compartment state, dose sessions, or notifications.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vision.reference_store import ReferenceStore
from vision.state_machine import StateTransition

LABELS = ("unchanged", "removed", "changed")
EXPECTED_CATEGORY = {"unchanged": "loaded", "removed": "removed", "changed": "changed"}
_CATEGORY = {
    "EXPECTED_PRESENT": "loaded",
    "OBJECT_RETURNED": "loaded",
    "REMOVAL_DETECTED": "removed",
    "EMPTY": "removed",
    "PARTIAL_OR_WRONG": "changed",
}


def state_category(state: str | None) -> str:
    """loaded / removed / changed for confirmed states; 'unclear' for anything else."""
    return _CATEGORY.get(state or "", "unclear")


def next_label(label: str) -> str:
    return LABELS[(LABELS.index(label) + 1) % len(LABELS)] if label in LABELS else LABELS[0]


def describe_labels(labels: dict[str, str]) -> str:
    changes = [f"{slot}-{label}" for slot, label in labels.items() if label != "unchanged"]
    return "_".join(changes) if changes else "no-change"


@dataclass
class SlotResult:
    slot: str
    label: str
    expected: str
    final_state: str
    got: str
    passed: bool
    confirmed_after_s: float | None
    final_diff_expected: float | None
    final_diff_empty: float | None


@dataclass
class TrialSummary:
    trial_id: str
    path: Path
    duration_s: float
    evaluations: int
    results: list[SlotResult] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def lines(self) -> list[str]:
        out = [f"Trial {self.trial_id}: {self.passed}/{len(self.results)} compartments correct, "
               f"{self.evaluations} evaluations over {self.duration_s:.1f}s"]
        for r in self.results:
            mark = "OK  " if r.passed else "MISS"
            when = f", confirmed at {r.confirmed_after_s:.1f}s" if r.confirmed_after_s is not None else ""
            out.append(f"  {mark} {r.slot}: did '{r.label}', camera says {r.final_state}{when} "
                       f"(diff vs loaded {r.final_diff_expected}, vs empty {r.final_diff_empty})")
        for f in self.flags:
            out.append(f"  ! {f}")
        return out


class TrialRecorder:
    def __init__(self, root: Path | str, slots: list[str], settings: dict[str, Any] | None = None,
                 save_frames: bool = True, jpeg_quality: int = 95) -> None:
        self.root = Path(root)
        self.slots = list(slots)
        self.settings = dict(settings or {})
        self.save_frames = save_frames
        self.jpeg_quality = int(jpeg_quality)
        self.labels: dict[str, str] = {s: "unchanged" for s in self.slots}
        self.change_threshold = float(self.settings.get("change_threshold") or 0.08)
        self._reset_run()

    def _reset_run(self) -> None:
        self.active = False
        self.trial_id: str | None = None
        self.trial_dir: Path | None = None
        self.started_mono: float | None = None
        self.started_at: str | None = None
        self.evaluations = 0
        self.first_scores: dict[str, dict[str, Any]] | None = None
        self.last_scores: dict[str, dict[str, Any]] = {}
        self.confirmed_after: dict[str, float] = {}
        self.start_mismatch_slots: list[str] = []
        self._meta: dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        """True once the first evaluation has checked the starting box: the tester may act ("GO").

        Acting before this point means the first evaluation sees the change instead of the
        calibrated starting state, and the trial cannot tell a correct start from a wrong one.
        """
        return self.active and self.evaluations > 0

    def cycle_label(self, slot: str) -> str:
        if not self.active and slot in self.labels:
            self.labels[slot] = next_label(self.labels[slot])
        return self.labels.get(slot, "unchanged")

    def elapsed(self, now_mono: float) -> float:
        return 0.0 if self.started_mono is None else now_mono - self.started_mono

    def start(self, now_mono: float, rois: dict[str, list[int]], ref_store: ReferenceStore,
              camera_index: int | None = None) -> str:
        if self.active:
            raise RuntimeError("A trial is already running")
        self._reset_run()
        stamp = datetime.now()
        base = f"{stamp.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^A-Za-z0-9_-]', '', describe_labels(self.labels))}"
        trial_dir = self.root / base
        suffix = 2
        while trial_dir.exists():
            trial_dir = self.root / f"{base}_{suffix}"
            suffix += 1
        (trial_dir / "frames").mkdir(parents=True)

        arrays: dict[str, np.ndarray] = {}
        for slot in self.slots:
            exp = ref_store.get_expected(slot)
            emp = ref_store.get_empty(slot)
            if exp is not None:
                arrays[f"expected_{slot}"] = exp
            if emp is not None:
                arrays[f"empty_{slot}"] = emp
        if arrays:
            np.savez_compressed(trial_dir / "references.npz", **arrays)

        self.active = True
        self.trial_id = trial_dir.name
        self.trial_dir = trial_dir
        self.started_mono = now_mono
        self.started_at = stamp.isoformat(timespec="seconds")
        self._meta = {
            "version": 1,
            "trial_id": self.trial_id,
            "started_at": self.started_at,
            "labels": dict(self.labels),
            "rois": {s: list(map(int, b)) for s, b in (rois or {}).items()},
            "camera_index": camera_index,
            "has_empty_reference": ref_store.has_empty(self.slots),
            "settings": self.settings,
            "status": "running",
        }
        self._write_meta()
        return self.trial_id

    def record(self, frame: np.ndarray | None, transitions: list[StateTransition], now_mono: float,
               source: str = "monitor") -> None:
        if not self.active or self.trial_dir is None or not transitions:
            return
        self.evaluations += 1
        t = round(self.elapsed(now_mono), 3)
        frame_name = None
        if self.save_frames and frame is not None and frame.size:
            frame_name = f"{self.evaluations:04d}.jpg"
            cv2.imwrite(str(self.trial_dir / "frames" / frame_name), frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])

        scores: dict[str, dict[str, Any]] = {}
        for tr in transitions:
            m = tr.metrics
            scores[tr.slot] = {
                "state": tr.new_state.value,
                "previous_state": tr.previous_state.value,
                "is_new_event": tr.is_new_event,
                "reason": tr.reason,
                "diff_from_expected": m.get("diff_from_expected"),
                "diff_from_empty": m.get("diff_from_empty"),
                "ssim_dissimilarity": m.get("ssim_dissimilarity"),
                "pixel_diff": m.get("pixel_diff"),
                "contours": m.get("contours"),
                "is_blurry": m.get("is_blurry"),
                "blur_score": m.get("blur_score"),
                "brightness": m.get("brightness"),
            }
            category = state_category(tr.new_state.value)
            if tr.slot not in self.confirmed_after and category in ("removed", "changed"):
                self.confirmed_after[tr.slot] = t

        if self.first_scores is None:
            self.first_scores = scores
            self.start_mismatch_slots = [
                s for s, sc in scores.items()
                if sc.get("diff_from_expected") is not None and sc["diff_from_expected"] >= self.change_threshold
            ]
        self.last_scores.update(scores)
        line = {"i": self.evaluations, "t": t, "source": source, "frame": frame_name, "slots": scores}
        with (self.trial_dir / "evaluations.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def finish(self, now_mono: float, final_states: dict[str, str]) -> TrialSummary:
        if not self.active or self.trial_dir is None or self.trial_id is None:
            raise RuntimeError("No trial is running")
        flags: list[str] = []
        off = list(self.start_mismatch_slots)
        if self.evaluations == 0:
            flags.append("No evaluations were recorded (scene never settled?). This trial cannot be graded.")
        if off:
            flags.append(f"At the first evaluation {', '.join(off)} already differed from calibration. "
                         "Set the box up as calibrated and wait for GO before acting.")
        if not self._meta.get("has_empty_reference"):
            flags.append("No empty reference (E): 'removed' and 'changed' cannot be told apart reliably.")

        results = []
        for slot in self.slots:
            label = self.labels.get(slot, "unchanged")
            final = final_states.get(slot, "UNKNOWN")
            got = state_category(final)
            expected = EXPECTED_CATEGORY[label]
            last = self.last_scores.get(slot, {})
            results.append(SlotResult(
                slot=slot, label=label, expected=expected, final_state=final, got=got,
                passed=(got == expected) and self.evaluations > 0,
                confirmed_after_s=self.confirmed_after.get(slot) if label != "unchanged" else None,
                final_diff_expected=last.get("diff_from_expected"),
                final_diff_empty=last.get("diff_from_empty"),
            ))

        summary = TrialSummary(self.trial_id, self.trial_dir, round(self.elapsed(now_mono), 2),
                               self.evaluations, results, flags)
        self._meta.update({
            "status": "finished" if self.evaluations else "no_data",
            "duration_s": summary.duration_s,
            "evaluations": self.evaluations,
            "results": [r.__dict__ for r in results],
            "passed": summary.passed,
            "flags": flags,
            "start_mismatch": bool(off),
        })
        self._write_meta()
        self._reset_run()
        return summary

    def _write_meta(self) -> None:
        if self.trial_dir is None:
            return
        (self.trial_dir / "trial.json").write_text(json.dumps(self._meta, indent=2), encoding="utf-8")
