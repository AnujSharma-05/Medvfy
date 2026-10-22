"""Replay recorded trials to measure and tune the decision thresholds.

The trial recorder saved, for every evaluation, the comparator scores of each
compartment (difference from the "loaded" and the "empty" calibration photos) plus
the time. Replaying those scores through a fresh CompartmentStateMachine with
different thresholds shows what each setting *would have* decided, without
re-running any physical test. Optionally the scores themselves can be recomputed
from the saved frames (`rescore`), which is how a change to the comparator is
measured on the same footage.

Error costs: a missed removal and a false removal are the two errors that would
mislead a caregiver (a dose reported taken that was not, or the reverse), so they
weigh 3x; other mistakes (wrong "changed" vs "loaded", unclear results) weigh 1x.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

from vision.comparator import VisualComparator
from vision.state_machine import CompartmentState, CompartmentStateMachine
from vision.trials import EXPECTED_CATEGORY, state_category

CHANGE_GRID = [0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.18, 0.22, 0.26, 0.30]
MATCH_GRID = [0.02, 0.03, 0.05, 0.07, 0.10, 0.13, 0.16, 0.20]
CATEGORIES = ["loaded", "removed", "changed", "unclear"]


@dataclass
class Trial:
    path: Path
    meta: dict[str, Any]
    evaluations: list[dict[str, Any]]

    @property
    def trial_id(self) -> str:
        return self.meta.get("trial_id", self.path.name)

    @property
    def labels(self) -> dict[str, str]:
        return self.meta.get("labels", {})


@dataclass
class Params:
    change_threshold: float
    match_threshold: float
    debounce_frames: int = 2
    confirm_seconds: float = 2.0


@dataclass
class Grade:
    params: Params
    total: int = 0
    correct: int = 0
    removal_missed: int = 0
    false_removal: int = 0
    other_errors: int = 0
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cost(self) -> int:
        return 3 * (self.removal_missed + self.false_removal) + self.other_errors

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def load_trials(root: Path | str, include_flagged: bool = False) -> tuple[list[Trial], list[str]]:
    """Finished trials under root. Returns (trials, skipped-with-reason)."""
    trials, skipped = [], []
    root = Path(root)
    if not root.exists():
        return trials, skipped
    for meta_path in sorted(root.glob("*/trial.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(f"{meta_path.parent.name}: unreadable ({exc})")
            continue
        if meta.get("status") != "finished":
            skipped.append(f"{meta_path.parent.name}: status {meta.get('status')}")
            continue
        if meta.get("start_mismatch") and not include_flagged:
            skipped.append(f"{meta_path.parent.name}: box did not match calibration at start")
            continue
        evals = []
        ev_path = meta_path.parent / "evaluations.jsonl"
        if ev_path.exists():
            for line in ev_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    evals.append(json.loads(line))
        trials.append(Trial(meta_path.parent, meta, evals))
    return trials, skipped


def replay(trial: Trial, params: Params) -> dict[str, str]:
    """Final state of each compartment if the trial had run with these parameters."""
    sm = CompartmentStateMachine(debounce_frames=params.debounce_frames, confirm_seconds=params.confirm_seconds)
    slots = list(trial.labels)
    for s in slots:
        sm.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
    for ev in trial.evaluations:
        for slot, sc in ev.get("slots", {}).items():
            if slot not in trial.labels:
                continue
            sm.evaluate(
                slot,
                diff_from_expected=sc.get("diff_from_expected"),
                diff_from_empty=sc.get("diff_from_empty"),
                is_blurry=bool(sc.get("is_blurry")),
                change_threshold=params.change_threshold,
                match_threshold=params.match_threshold,
                observed_at=float(ev.get("t", 0.0)),
            )
    return {s: sm.get_state(s).value for s in slots}


def grade(trials: Iterable[Trial], params: Params) -> Grade:
    g = Grade(params, confusion={label: {c: 0 for c in CATEGORIES} for label in EXPECTED_CATEGORY})
    for trial in trials:
        finals = replay(trial, params)
        for slot, label in trial.labels.items():
            expected = EXPECTED_CATEGORY.get(label)
            if expected is None:
                continue
            got = state_category(finals.get(slot))
            g.total += 1
            g.confusion[label][got] += 1
            if got == expected:
                g.correct += 1
                continue
            if label == "removed":
                g.removal_missed += 1
            elif got == "removed":
                g.false_removal += 1
            else:
                g.other_errors += 1
            g.failures.append({"trial": trial.trial_id, "slot": slot, "label": label, "got": finals.get(slot)})
    return g


def sweep(trials: list[Trial], base: Params,
          change_grid: list[float] = CHANGE_GRID, match_grid: list[float] = MATCH_GRID) -> list[Grade]:
    """Grade every (change, match) pair with match < change; best first."""
    grades = []
    for change in change_grid:
        for match in match_grid:
            if match >= change:
                continue
            grades.append(grade(trials, Params(change, match, base.debounce_frames, base.confirm_seconds)))
    # Lowest cost first; ties broken toward the setting closest to the current one (less churn).
    grades.sort(key=lambda g: (g.cost, abs(g.params.change_threshold - base.change_threshold)
                               + abs(g.params.match_threshold - base.match_threshold)))
    return grades


def separability(trials: Iterable[Trial]) -> dict[str, dict[str, dict[str, float]]]:
    """Distribution of the final scores per label: shows whether any threshold can separate them."""
    values: dict[str, dict[str, list[float]]] = {}
    for trial in trials:
        if not trial.evaluations:
            continue
        last = trial.evaluations[-1].get("slots", {})
        for slot, label in trial.labels.items():
            sc = last.get(slot, {})
            for key in ("diff_from_expected", "diff_from_empty"):
                v = sc.get(key)
                if isinstance(v, (int, float)):
                    values.setdefault(label, {}).setdefault(key, []).append(float(v))
    out: dict[str, dict[str, dict[str, float]]] = {}
    for label, by_key in values.items():
        out[label] = {}
        for key, vals in by_key.items():
            out[label][key] = {"n": len(vals), "min": round(min(vals), 4),
                               "median": round(statistics.median(vals), 4), "max": round(max(vals), 4)}
    return out


def rescore(trial: Trial, comparator: VisualComparator | None = None) -> Trial:
    """Recompute every evaluation's scores from the saved frames with the current comparator."""
    comparator = comparator or VisualComparator()
    refs_path = trial.path / "references.npz"
    if not refs_path.exists():
        return trial
    with np.load(refs_path, allow_pickle=False) as data:
        refs = {k: data[k].copy() for k in data.files}
    rois = trial.meta.get("rois", {})
    new_evals = []
    for ev in trial.evaluations:
        frame_name = ev.get("frame")
        frame = cv2.imread(str(trial.path / "frames" / frame_name)) if frame_name else None
        if frame is None:
            new_evals.append(ev)
            continue
        slots = {}
        for slot, sc in ev.get("slots", {}).items():
            x, y, w, h = rois.get(slot, [0, 0, 0, 0])
            crop = frame[y : y + h, x : x + w]
            exp, emp = refs.get(f"expected_{slot}"), refs.get(f"empty_{slot}")
            new = dict(sc)
            if exp is not None and crop.size:
                new["diff_from_expected"] = comparator.compare(exp, crop).composite_diff
                new["diff_from_empty"] = comparator.compare(emp, crop).composite_diff if emp is not None else None
            slots[slot] = new
        new_evals.append({**ev, "slots": slots})
    return Trial(trial.path, trial.meta, new_evals)
