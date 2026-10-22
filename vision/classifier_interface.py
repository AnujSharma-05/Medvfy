"""Medication Object and Quantity Detection Interface.

Provides a clean architectural interface separating:
1. Compartment state verification (changed vs unchanged)
2. Visible object counting and bounding (morphological analysis)
3. Medication chemical/brand identification (requires labelled datasets)

CRITICAL PRODUCT BOUNDARY:
Never invent a medication identity or dosage from insufficient visual evidence.
Classical appearance heuristics report shape/color morphology only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence
import cv2
import numpy as np


@dataclass
class DetectedObject:
    bounding_box: tuple[int, int, int, int]  # x, y, w, h
    area: float
    color_family: str
    aspect_ratio: float
    confidence: float
    provisional_label: str


@dataclass
class MedicationClassificationResult:
    slot: str
    detected_count: int
    expected_count: int | None
    count_matches: bool
    objects: list[DetectedObject] = field(default_factory=list)
    identification_confidence: float = 0.0
    notes: str = ""


class BaseMedicationClassifier(ABC):
    """Abstract interface for pill detection, counting, and future classification."""

    @abstractmethod
    def classify(
        self,
        crop: np.ndarray,
        slot: str = "A",
        expected_count: int | None = None,
    ) -> MedicationClassificationResult:
        """Analyze a compartment crop for distinct objects and estimated count."""
        pass


class ClassicalHeuristicClassifier(BaseMedicationClassifier):
    """Classical morphological classifier estimating visible item count and basic morphology."""

    def __init__(self, min_area: int = 40, max_area: int = 5000) -> None:
        self.min_area = min_area
        self.max_area = max_area

    def estimate_color(self, bgr_crop: np.ndarray) -> str:
        """Estimate basic color family from mean HSV."""
        if bgr_crop.size == 0:
            return "unknown"
        hsv = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2HSV)
        h, s, v = np.mean(hsv[:, :, 0]), np.mean(hsv[:, :, 1]), np.mean(hsv[:, :, 2])

        if v < 40:
            return "black/dark"
        if s < 35 and v > 180:
            return "white"
        if s < 40:
            return "gray"
        if h < 10 or h > 165:
            return "red/pink"
        if 10 <= h < 25:
            return "orange"
        if 25 <= h < 35:
            return "yellow"
        if 35 <= h < 85:
            return "green"
        if 85 <= h < 130:
            return "blue"
        if 130 <= h < 165:
            return "purple"
        return "unspecified"

    def classify(
        self,
        crop: np.ndarray,
        slot: str = "A",
        expected_count: int | None = None,
    ) -> MedicationClassificationResult:
        if crop is None or crop.size == 0:
            return MedicationClassificationResult(
                slot=slot,
                detected_count=0,
                expected_count=expected_count,
                count_matches=(expected_count == 0 if expected_count is not None else False),
                notes="Invalid or empty image crop",
            )

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Otsu thresholding for foreground separation: support both light pills on dark bg
        # and dark pills on light bg by selecting the minority foreground
        _, thresh_light = cv2.threshold(
            blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        _, thresh_dark = cv2.threshold(
            blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        c_light = cv2.countNonZero(thresh_light)
        c_dark = cv2.countNonZero(thresh_dark)
        thresh = thresh_light if c_light < c_dark else thresh_dark

        kernel = np.ones((3, 3), np.uint8)
        opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

        contours, _ = cv2.findContours(
            opening, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        detected_objects: list[DetectedObject] = []
        for cnt in contours:
            area = float(cv2.contourArea(cnt))
            if self.min_area <= area <= self.max_area:
                x, y, w, h = cv2.boundingRect(cnt)
                aspect = float(w) / float(h) if h > 0 else 1.0
                patch = crop[y : y + h, x : x + w]
                color = self.estimate_color(patch)

                # Classical vision cannot verify pharmaceutical identity
                label = f"token_shape_{color}_{'elongated' if aspect > 1.3 or aspect < 0.7 else 'compact'}"
                detected_objects.append(
                    DetectedObject(
                        bounding_box=(x, y, w, h),
                        area=area,
                        color_family=color,
                        aspect_ratio=round(aspect, 2),
                        confidence=0.5,  # Explicit conservative confidence
                        provisional_label=label,
                    )
                )

        count = len(detected_objects)
        count_matches = (count == expected_count) if expected_count is not None else True

        notes = (
            f"Detected {count} visible token(s). "
            "Classical morphology only; chemical medication identity not established."
        )

        return MedicationClassificationResult(
            slot=slot,
            detected_count=count,
            expected_count=expected_count,
            count_matches=count_matches,
            objects=detected_objects,
            identification_confidence=0.5 if count > 0 else 0.0,
            notes=notes,
        )
