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

import numpy as np

from vision.comparator import VisualComparator
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
        return MonitorTick(EVALUATED, motion=motion, transitions=transitions)

    def _interrupt(self) -> None:
        self._stable_since = None
        for slot in self.slots:
            self.state_machine.reset_candidate(slot)

    def blur_limit(self) -> float:
        """Sharpness below which a frame is too blurry, relative to calibration when known."""
        calibrated = self.ref_store.metadata.get("calibration_sharpness")
        if isinstance(calibrated, (int, float)) and calibrated > 0:
            return float(calibrated) * self.config.blur_min_ratio
        return self.config.blur_threshold

    def frame_unusable(self, quality: Any) -> bool:
        exposure_bad = quality.is_underexposed or quality.is_overexposed or quality.blur_score == 0.0
        return exposure_bad or quality.blur_score < self.blur_limit()

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

        transitions: list[StateTransition] = []
        for slot in self.slots:
            expected_crop = self.ref_store.get_expected(slot)
            empty_crop = self.ref_store.get_empty(slot)
            current_crop = crops.get(slot)

            metrics_exp = self.comparator.compare(expected_crop, current_crop)
            # The comparator scores missing input as maximally different (1.0); that is lack of
            # evidence, not evidence of removal, so pass None and let the state machine say UNCERTAIN.
            evidence_missing = expected_crop is None or current_crop is None or current_crop.size == 0
            diff_expected = None if evidence_missing else metrics_exp.composite_diff
            diff_empty = None
            if empty_crop is not None and not evidence_missing:
                diff_empty = self.comparator.compare(empty_crop, current_crop).composite_diff

            transitions.append(
                self.state_machine.evaluate(
                    slot=slot,
                    diff_from_expected=diff_expected,
                    diff_from_empty=diff_empty,
                    is_blurry=is_blurry,
                    change_threshold=self.config.change_threshold,
                    match_threshold=self.config.match_threshold,
                    extra_metrics={
                        "composite_diff": metrics_exp.composite_diff,
                        "ssim_dissimilarity": metrics_exp.structural_dissimilarity,
                        "pixel_diff": metrics_exp.pixel_diff_fraction,
                        "contours": metrics_exp.contour_count,
                        "quality": quality.reason,
                        "is_blurry": is_blurry,
                        "blur_score": quality.blur_score,
                        "brightness": quality.brightness_score,
                    },
                    observed_at=observed_at,
                )
            )
        return transitions
