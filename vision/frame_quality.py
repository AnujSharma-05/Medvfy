"""Frame Quality Assessment for MedVerify.

Provides deterministic methods to verify:
- Image presence and shape validity
- Sharpness / blur rejection using Laplacian variance
- Extreme illumination rejection (under/over exposure)
- Inter-frame motion stability detection
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
import cv2
import numpy as np


@dataclass
class FrameQualityResult:
    is_acceptable: bool
    is_blurry: bool
    blur_score: float
    brightness_score: float
    is_underexposed: bool
    is_overexposed: bool
    reason: str


class FrameQualityAssessor:
    """Evaluates image frame quality to prevent decisions based on degraded frames."""

    def __init__(
        self,
        blur_threshold: float = 60.0,
        min_brightness: float = 20.0,
        max_brightness: float = 235.0,
        motion_threshold: float = 0.04,
    ) -> None:
        self.blur_threshold = float(blur_threshold)
        self.min_brightness = float(min_brightness)
        self.max_brightness = float(max_brightness)
        self.motion_threshold = float(motion_threshold)

    def compute_blur_score(self, image: np.ndarray) -> float:
        """Calculate focus measure using variance of the Laplacian."""
        if image is None or image.size == 0:
            return 0.0
        gray = (
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if len(image.shape) == 3
            else image
        )
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def compute_brightness_score(self, image: np.ndarray) -> float:
        """Calculate mean image luminance."""
        if image is None or image.size == 0:
            return 0.0
        gray = (
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if len(image.shape) == 3
            else image
        )
        return float(np.mean(gray))

    def assess(self, image: np.ndarray) -> FrameQualityResult:
        """Assess whether a frame meets basic quality requirements for analysis."""
        if image is None or image.size == 0 or len(image.shape) < 2:
            return FrameQualityResult(
                is_acceptable=False,
                is_blurry=True,
                blur_score=0.0,
                brightness_score=0.0,
                is_underexposed=True,
                is_overexposed=False,
                reason="Frame is empty or invalid",
            )

        blur_score = self.compute_blur_score(image)
        brightness = self.compute_brightness_score(image)

        is_blurry = blur_score < self.blur_threshold
        is_underexposed = brightness < self.min_brightness
        is_overexposed = brightness > self.max_brightness

        if is_blurry:
            reason = f"Image is blurry (focus score {blur_score:.1f} < {self.blur_threshold:.1f})"
        elif is_underexposed:
            reason = f"Image is underexposed/too dark (brightness {brightness:.1f} < {self.min_brightness:.1f})"
        elif is_overexposed:
            reason = f"Image is overexposed/washed out (brightness {brightness:.1f} > {self.max_brightness:.1f})"
        else:
            reason = "Frame quality acceptable"

        is_acceptable = not (is_blurry or is_underexposed or is_overexposed)

        return FrameQualityResult(
            is_acceptable=is_acceptable,
            is_blurry=is_blurry,
            blur_score=round(blur_score, 2),
            brightness_score=round(brightness, 2),
            is_underexposed=is_underexposed,
            is_overexposed=is_overexposed,
            reason=reason,
        )

    def check_motion_stability(
        self, frames: Sequence[np.ndarray]
    ) -> tuple[bool, float]:
        """Check whether consecutive frames indicate a stable, non-moving scene.

        Returns:
            (is_stable, max_motion_fraction)
        """
        if len(frames) < 2:
            return True, 0.0

        max_motion = 0.0
        for i in range(len(frames) - 1):
            f1, f2 = frames[i], frames[i + 1]
            if f1.shape != f2.shape:
                return False, 1.0

            g1 = cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY) if len(f1.shape) == 3 else f1
            g2 = cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY) if len(f2.shape) == 3 else f2

            diff = cv2.absdiff(g1, g2)
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            motion = float(cv2.countNonZero(thresh) / thresh.size)
            if motion > max_motion:
                max_motion = motion

        is_stable = max_motion <= self.motion_threshold
        return is_stable, round(max_motion, 4)
