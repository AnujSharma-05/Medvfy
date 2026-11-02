"""Continuous Compartment Monitoring for MedVerify.

Turns a stream of camera frames into periodic, gated state-machine evaluations.

Why a gate: when a person reaches into the box, every frame differs from the
references, but none of those frames is evidence about what the compartment
contains. Evaluating them would produce false candidates (or UNCERTAIN churn).
The monitor therefore only evaluates once the scene has been still for a settle
period, and interrupts any pending candidate while the scene is moving, so the
unobserved period never counts toward confirming a removal or return.

This module has no camera, window, or network dependencies; callers supply frames
and monotonic timestamps, which keeps the timing behaviour deterministic in tests.

SAFETY BOUNDARY:
A confirmed removal means the visible contents changed. It does not prove ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from vision.comparator import VisualComparator
from vision.content import CountTracker, medicine_mask
from vision.frame_quality import FrameQualityAssessor
from vision.reference_store import ReferenceStore
from vision.state_machine import CompartmentStateMachine, StateTransition


@dataclass
class MonitorConfig:
    eval_interval_s: float = 0.5   # Minimum time between evaluations while the scene is still
    settle_s: float = 1.0          # Scene must be still this long before the first evaluation
    motion_threshold: float = 0.02  # Fraction of ROI pixels changing between frames that counts as motion
    change_threshold: float = 0.08
    match_threshold: float = 0.05
    # Sharpness (variance of the Laplacian) depends on how much texture the scene has: a plain
    # page with a few objects scores low even when perfectly in focus. So a frame counts as
    # blurry when it falls below this fraction of the sharpness measured at calibration (S).
    # The absolute threshold is only a fallback for calibrations made before this was recorded.
    blur_min_ratio: float = 0.5
    blur_threshold: float = 60.0
    # "auto": judge by how much medicine is present (area vs the empty photo) whenever an empty
    # photo exists, else by pixel comparison. Area does not change when pills are rearranged or
    # put back, so it is the robust choice for real handling (see vision/content.py).
    evidence: str = "auto"
    content_match_threshold: float = 0.15   # area within 15% of "loaded" (and same colour) = loaded
    content_change_threshold: float = 0.30  # area off by 30% or more = clearly changed
    confirm_observations: int = 2
    confirm_seconds: float = 2.0

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "MonitorConfig":
        defaults = cls()
        return cls(
            eval_interval_s=float(settings.get("monitor_interval_seconds", defaults.eval_interval_s)),
            settle_s=float(settings.get("settle_seconds", defaults.settle_s)),
            motion_threshold=float(settings.get("motion_threshold", defaults.motion_threshold)),
            change_threshold=float(settings.get("change_threshold", defaults.change_threshold)),
            match_threshold=float(settings.get("match_threshold", defaults.match_threshold)),
            blur_min_ratio=float(settings.get("blur_min_ratio", defaults.blur_min_ratio)),
            blur_threshold=float(settings.get("blur_threshold", defaults.blur_threshold)),
            evidence=str(settings.get("evidence", defaults.evidence)),
            content_match_threshold=float(settings.get("content_match_threshold", defaults.content_match_threshold)),
            content_change_threshold=float(settings.get("content_change_threshold",
                                                        defaults.content_change_threshold)),
            confirm_observations=int(settings.get("debounce_frames", defaults.confirm_observations)),
            confirm_seconds=float(settings.get("confirm_seconds", defaults.confirm_seconds)),
        )


# Tick statuses
NOT_READY = "NOT_READY"    # No regions or expected references; nothing can be evaluated
MOVING = "MOVING"          # Scene is changing between frames; decision deferred
SETTLING = "SETTLING"      # Scene is still, but not yet for settle_s
WAITING = "WAITING"        # Still and settled; waiting for the next evaluation slot
EVALUATED = "EVALUATED"    # State machine evaluated every compartment on this frame


@dataclass
class MonitorTick:
    status: str
    motion: float = 0.0
    transitions: list[StateTransition] = field(default_factory=list)
    message: str = ""
    count_changes: list[tuple[str, int, int]] = field(default_factory=list)  # (slot, old, new)

    @property
    def new_events(self) -> list[StateTransition]:
        return [t for t in self.transitions if t.is_new_event]


class ContinuousMonitor:
    """Gates per-frame evidence on scene stability and evaluates compartments over time."""

    def __init__(
        self,
        slots: list[str],
        ref_store: ReferenceStore,
        state_machine: CompartmentStateMachine,
        config: MonitorConfig | None = None,
        comparator: VisualComparator | None = None,
        assessor: FrameQualityAssessor | None = None,
    ) -> None:
        self.slots = list(slots)
        self.ref_store = ref_store
        self.state_machine = state_machine
        self.config = config or MonitorConfig()
        self.comparator = comparator or VisualComparator()
        self.assessor = assessor or FrameQualityAssessor(motion_threshold=self.config.motion_threshold)
        self.rois: dict[str, list[int]] | None = None
        self.last_quality: Any = None

        self._previous_crops: dict[str, np.ndarray] | None = None
        self._stable_since: float | None = None
        self._last_eval: float | None = None
        self._ref_key: Any = object()
        self._ref_stats: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, CountTracker] = {}
        self.last_count_changes: list[tuple[str, int, int]] = []

    def reset(self) -> None:
        """Forget timing and motion history (call after calibration, ROI, or camera changes)."""
        self._previous_crops = None
        self._stable_since = None
        self._last_eval = None

    def is_ready(self) -> bool:
        return bool(self.rois) and self.ref_store.has_expected(self.slots)

    def crop(self, frame: np.ndarray) -> dict[str, np.ndarray]:
        crops: dict[str, np.ndarray] = {}
        for slot, (x, y, w, h) in (self.rois or {}).items():
            crops[slot] = frame[y : y + h, x : x + w].copy()
        return crops

    def _measure_motion(self, crops: dict[str, np.ndarray]) -> float | None:
        """Largest per-compartment motion fraction since the previous frame, or None if unknown."""
        if self._previous_crops is None:
            return None
        max_motion = 0.0
        for slot, crop in crops.items():
            prev = self._previous_crops.get(slot)
            if prev is None or prev.shape != crop.shape:
                return None
            _, motion = self.assessor.check_motion_stability([prev, crop])
            max_motion = max(max_motion, motion)
        return max_motion

    def process(self, frame: np.ndarray, now: float) -> MonitorTick:
        """Feed one camera frame captured at monotonic time `now`."""
        if not self.is_ready():
            self.reset()
            return MonitorTick(NOT_READY, message="Monitoring needs regions (R) and an expected reference (S).")
        if frame is None or frame.size == 0:
            self._interrupt()
            return MonitorTick(MOVING, motion=1.0, message="No usable frame; decision deferred.")

        crops = self.crop(frame)
        motion = self._measure_motion(crops)
        self._previous_crops = crops

        if motion is None or motion > self.config.motion_threshold:
            # Moving (or no history to judge): evidence is unusable and the clock restarts.
            self._interrupt()
            return MonitorTick(
                MOVING,
                motion=1.0 if motion is None else motion,
                message="Scene moving; waiting for it to settle.",
            )

        if self._stable_since is None:
            self._stable_since = now
        if now - self._stable_since < self.config.settle_s:
            return MonitorTick(SETTLING, motion=motion, message="Scene still; settling before evaluation.")
        if self._last_eval is not None and now - self._last_eval < self.config.eval_interval_s:
            return MonitorTick(WAITING, motion=motion)

        self._last_eval = now
        transitions = self.evaluate(frame, crops, observed_at=now)
        return MonitorTick(EVALUATED, motion=motion, transitions=transitions,
                           count_changes=list(self.last_count_changes))

    def _interrupt(self) -> None:
        self._stable_since = None
        for slot in self.slots:
            self.state_machine.reset_candidate(slot)
        for counter in self.counters.values():
            counter.interrupt()

    def blur_limit(self) -> float:
        """Sharpness below which a frame is too blurry, relative to calibration when known.

        Taking medicine away removes edges, so an emptied box is legitimately less "sharp" than a
        loaded one (test sheet: loaded ~230, empty ~73). The limit therefore follows the LOWER of
        the loaded and empty calibration sharpness; otherwise emptying a compartment would read
        as a blurry camera and the removal would never be confirmed.
        """
        known = [float(v) for v in (self.ref_store.metadata.get("calibration_sharpness"),
                                    self.ref_store.metadata.get("empty_sharpness"))
                 if isinstance(v, (int, float)) and v > 0]
        if known:
            return min(known) * self.config.blur_min_ratio
        return self.config.blur_threshold

    def frame_unusable(self, quality: Any) -> bool:
        exposure_bad = quality.is_underexposed or quality.is_overexposed or quality.blur_score == 0.0
        return exposure_bad or quality.blur_score < self.blur_limit()

    def reference_stats(self) -> dict[str, dict[str, Any]]:
        """Per compartment, from the loaded and empty photos: medicine area, colour, and (when a
        pill count was given at calibration) the area of one pill. Recomputed after recalibration."""
        key = (self.ref_store.metadata.get("updated_at"), tuple(sorted(self.ref_store.expected)),
               tuple(sorted(self.ref_store.empty)), str(self.ref_store.metadata.get("loaded_counts")))
        if key == self._ref_key:
            return self._ref_stats
        counts = self.ref_store.metadata.get("loaded_counts") or {}
        stats: dict[str, dict[str, Any]] = {}
        for slot in self.slots:
            exp, emp = self.ref_store.get_expected(slot), self.ref_store.get_empty(slot)
            if exp is None or emp is None or exp.shape != emp.shape:
                continue
            mask = medicine_mask(exp, emp)
            area = int(np.count_nonzero(mask))
            n = int(counts.get(slot) or 0)
            stats[slot] = {"area": area, "color": _mean_color(exp, mask), "count": n or None,
                           "unit": (area / n) if n and area else None}
        self._ref_key, self._ref_stats = key, stats
        # New calibration: counts start from what was declared, nothing to confirm yet.
        self.counters = {
            slot: CountTracker(self.config.confirm_observations, self.config.confirm_seconds)
            for slot, st in stats.items() if st["unit"]
        }
        for slot, counter in self.counters.items():
            counter.reset(stats[slot]["count"])
        return stats

    def counts(self) -> dict[str, int | None]:
        """Confirmed pill count per compartment that was calibrated with a count."""
        self.reference_stats()
        return {slot: c.confirmed for slot, c in self.counters.items()}

    def loaded_counts(self) -> dict[str, int]:
        return {slot: st["count"] for slot, st in self.reference_stats().items() if st["count"]}

    def uses_content(self) -> bool:
        return self.config.evidence != "pixels" and bool(self.reference_stats())

    def evaluate(
        self,
        frame: np.ndarray,
        crops: dict[str, np.ndarray] | None = None,
        observed_at: float | None = None,
    ) -> list[StateTransition]:
        """Evaluate every compartment on one frame, without the stability gate.

        Used by the gated loop above and by the manual compare key.
        """
        quality = self.assessor.assess(frame)
        self.last_quality = quality
        is_blurry = self.frame_unusable(quality)
        crops = crops if crops is not None else self.crop(frame)
        stats = self.reference_stats() if self.config.evidence != "pixels" else {}
        now = observed_at if observed_at is not None else 0.0
        self.last_count_changes = []

        transitions: list[StateTransition] = []
        for slot in self.slots:
            expected_crop = self.ref_store.get_expected(slot)
            empty_crop = self.ref_store.get_empty(slot)
            current_crop = crops.get(slot)
            # Missing input is lack of evidence, not evidence of removal: pass None and let the
            # state machine say UNCERTAIN.
            evidence_missing = expected_crop is None or current_crop is None or current_crop.size == 0
            extra: dict[str, Any] = {"quality": quality.reason, "is_blurry": is_blurry,
                                     "blur_score": quality.blur_score, "brightness": quality.brightness_score}
            ref = stats.get(slot)

            if (ref and ref["area"] > 0 and not evidence_missing and empty_crop is not None
                    and current_crop.shape == empty_crop.shape):
                # Content evidence: how much medicine, and is it the same colour as calibrated.
                mask = medicine_mask(current_crop, empty_crop)
                area = int(np.count_nonzero(mask))
                ratio = area / ref["area"]
                shift = 0.0
                if area >= 0.2 * ref["area"]:
                    shift = float(np.linalg.norm(_mean_color(current_crop, mask) - ref["color"]))
                diff_expected = min(1.0, max(abs(1.0 - ratio), shift / 40.0))
                diff_empty = min(1.0, ratio)
                match_t, change_t = self.config.content_match_threshold, self.config.content_change_threshold
                extra.update({"composite_diff": round(diff_expected, 4), "area": area,
                              "area_ratio": round(ratio, 3), "color_shift": round(shift, 1),
                              "evidence": "content"})
                counter = self.counters.get(slot)
                if counter is not None:
                    estimate = area / ref["unit"]
                    extra["count_estimate"] = round(estimate, 2)
                    change = counter.update(None if is_blurry else estimate, now)
                    if change is not None:
                        self.last_count_changes.append((slot, change[0], change[1]))
            else:
                metrics_exp = self.comparator.compare(expected_crop, current_crop)
                diff_expected = None if evidence_missing else metrics_exp.composite_diff
                diff_empty = None
                if empty_crop is not None and not evidence_missing:
                    diff_empty = self.comparator.compare(empty_crop, current_crop).composite_diff
                match_t, change_t = self.config.match_threshold, self.config.change_threshold
                extra.update({"composite_diff": metrics_exp.composite_diff,
                              "ssim_dissimilarity": metrics_exp.structural_dissimilarity,
                              "pixel_diff": metrics_exp.pixel_diff_fraction,
                              "contours": metrics_exp.contour_count, "evidence": "pixels"})

            transitions.append(
                self.state_machine.evaluate(
                    slot=slot,
                    diff_from_expected=diff_expected,
                    diff_from_empty=diff_empty,
                    is_blurry=is_blurry,
                    change_threshold=change_t,
                    match_threshold=match_t,
                    extra_metrics=extra,
                    observed_at=observed_at,
                )
            )
        return transitions


def _mean_color(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Mean Lab colour (a, b) of the masked pixels: tells one medicine's colour from another's."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    sel = lab[mask > 0]
    if sel.size == 0:
        return np.zeros(2, np.float32)
    return sel[:, 1:3].mean(axis=0)
