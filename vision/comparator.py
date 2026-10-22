"""Visual State Comparator for MedVerify.

Combines illumination normalization, structural similarity (SSIM),
color histogram correlation, and contour/connected-component analysis
to provide robust visual evidence for compartment state evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple
import cv2
import numpy as np


@dataclass
class ComparisonMetrics:
    composite_diff: float
    pixel_diff_fraction: float
    structural_dissimilarity: float
    color_correlation: float
    contour_count: int
    total_contour_area: float
    is_lighting_shift: bool
    is_shift_only: bool


class VisualComparator:
    """Extracts multi-metric visual evidence comparing observation to reference."""

    def __init__(
        self,
        pixel_threshold: int = 28,
        min_contour_area: int = 30,
    ) -> None:
        self.pixel_threshold = int(pixel_threshold)
        self.min_contour_area = int(min_contour_area)

    def normalize_illumination(self, image: np.ndarray) -> np.ndarray:
        """Normalize luminance across frames using CLAHE on LAB color space."""
        if image is None or image.size == 0:
            return image
        if len(image.shape) == 2:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
            return clahe.apply(image)

        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        l_norm = clahe.apply(l)
        lab_norm = cv2.merge((l_norm, a, b))
        return cv2.cvtColor(lab_norm, cv2.COLOR_LAB2BGR)

    def compute_ssim(self, img1_gray: np.ndarray, img2_gray: np.ndarray) -> float:
        """Compute Structural Similarity Index (SSIM) between two grayscale images."""
        if img1_gray.shape != img2_gray.shape:
            img2_gray = cv2.resize(
                img2_gray, (img1_gray.shape[1], img1_gray.shape[0])
            )

        c1 = (0.01 * 255) ** 2
        c2 = (0.03 * 255) ** 2

        img1 = img1_gray.astype(np.float64)
        img2 = img2_gray.astype(np.float64)

        mu1 = cv2.GaussianBlur(img1, (11, 11), 1.5)
        mu2 = cv2.GaussianBlur(img2, (11, 11), 1.5)

        mu1_sq = mu1 * mu1
        mu2_sq = mu2 * mu2
        mu1_mu2 = mu1 * mu2

        sigma1_sq = cv2.GaussianBlur(img1 * img1, (11, 11), 1.5) - mu1_sq
        sigma2_sq = cv2.GaussianBlur(img2 * img2, (11, 11), 1.5) - mu2_sq
        sigma12 = cv2.GaussianBlur(img1 * img2, (11, 11), 1.5) - mu1_mu2

        ssim_map = ((2 * mu1_mu2 + c1) * (2 * sigma12 + c2)) / (
            (mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2)
        )
        return float(np.clip(np.mean(ssim_map), -1.0, 1.0))

    def compute_color_correlation(
        self, img1: np.ndarray, img2: np.ndarray
    ) -> float:
        """Compute color histogram correlation in HSV space (1.0 = identical distribution)."""
        if img1.shape != img2.shape:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

        hsv1 = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
        hsv2 = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)

        hist1 = cv2.calcHist([hsv1], [0, 1], None, [16, 16], [0, 180, 0, 256])
        hist2 = cv2.calcHist([hsv2], [0, 1], None, [16, 16], [0, 180, 0, 256])

        cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)

        score = float(cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL))
        return max(0.0, score)

    def extract_foreground_mask(
        self, before_gray: np.ndarray, after_gray: np.ndarray
    ) -> np.ndarray:
        """Extract morphological foreground change mask."""
        if before_gray.shape != after_gray.shape:
            after_gray = cv2.resize(
                after_gray, (before_gray.shape[1], before_gray.shape[0])
            )

        diff = cv2.absdiff(before_gray, after_gray)
        diff_blur = cv2.GaussianBlur(diff, (5, 5), 0)
        _, mask = cv2.threshold(
            diff_blur, self.pixel_threshold, 255, cv2.THRESH_BINARY
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)
        )
        return mask

    def analyze_contours(
        self, mask: np.ndarray
    ) -> Tuple[int, float, list[np.ndarray]]:
        """Find significant contours, counting objects and total foreground area."""
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        significant = [
            cnt for cnt in contours if cv2.contourArea(cnt) >= self.min_contour_area
        ]
        total_area = sum(cv2.contourArea(cnt) for cnt in significant)
        return len(significant), float(total_area), significant

    def detect_blobs(self, img: np.ndarray) -> list[tuple[float, tuple[int, int]]]:
        """Detect foreground object blobs in compartment.
        Returns list of (area, (center_x, center_y)).
        """
        if img is None or img.size == 0:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Separate foreground from background using Otsu
        _, thresh_light = cv2.threshold(
            blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        _, thresh_dark = cv2.threshold(
            blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )

        c_light = cv2.countNonZero(thresh_light)
        c_dark = cv2.countNonZero(thresh_dark)
        # Select smaller foreground assuming medication objects occupy minority of area
        thresh = thresh_light if c_light < c_dark else thresh_dark

        kernel = np.ones((3, 3), np.uint8)
        opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(
            opening, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        blobs: list[tuple[float, tuple[int, int]]] = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area >= self.min_contour_area:
                m = cv2.moments(c)
                if m["m00"] > 0:
                    cx = int(m["m10"] / m["m00"])
                    cy = int(m["m01"] / m["m00"])
                    blobs.append((area, (cx, cy)))
        return blobs

    def compare(
        self, before: np.ndarray, after: np.ndarray
    ) -> ComparisonMetrics:
        """Perform comprehensive visual comparison between two compartment crops.

        Args:
            before: Reference image (e.g. expected medication baseline)
            after: Current observation image
        """
        if before is None or after is None or before.size == 0 or after.size == 0:
            return ComparisonMetrics(
                composite_diff=1.0,
                pixel_diff_fraction=1.0,
                structural_dissimilarity=1.0,
                color_correlation=0.0,
                contour_count=0,
                total_contour_area=0.0,
                is_lighting_shift=False,
                is_shift_only=False,
            )

        if before.shape != after.shape:
            after = cv2.resize(after, (before.shape[1], before.shape[0]))

        # Detect discrete objects in both frames
        blobs_before = self.detect_blobs(before)
        blobs_after = self.detect_blobs(after)

        # Normalize illumination
        b_norm = self.normalize_illumination(before)
        a_norm = self.normalize_illumination(after)

        b_gray = cv2.cvtColor(b_norm, cv2.COLOR_BGR2GRAY)
        a_gray = cv2.cvtColor(a_norm, cv2.COLOR_BGR2GRAY)

        # 1. Structural Similarity
        ssim_raw = self.compute_ssim(b_gray, a_gray)
        structural_dissimilarity = float(np.clip((1.0 - ssim_raw) / 2.0, 0.0, 1.0))

        # 2. Pixel difference fraction on normalized images
        mask = self.extract_foreground_mask(b_gray, a_gray)
        pixel_diff = float(cv2.countNonZero(mask) / max(1, mask.size))

        # 3. Color distribution correlation
        color_corr = self.compute_color_correlation(before, after)

        # 4. Foreground contours from diff mask
        contour_count, total_area, _ = self.analyze_contours(mask)

        # 5. Composite difference calculation
        composite = 0.5 * pixel_diff + 0.5 * structural_dissimilarity

        # Determine object addition / removal vs shift vs lighting change
        count_before = len(blobs_before)
        count_after = len(blobs_after)

        is_shift_only = False
        is_lighting_shift = False

        if count_before != count_after:
            # Clear object addition or removal: boost composite difference to reflect count change.
            # The contour term is capped at 1.0 so the boost cannot push the score off the
            # [0, 1] scale that downstream thresholds (match/change) are defined on.
            contour_fraction = total_area / max(1, mask.size)
            composite = max(composite, 0.15, min(1.0, contour_fraction * 2.0))
        elif count_before > 0 and count_after > 0:
            # Both contain objects: check if positions shifted or colors changed
            b_centers = sorted([b[1] for b in blobs_before])
            a_centers = sorted([b[1] for b in blobs_after])
            max_dist = max(
                np.hypot(b[0] - a[0], b[1] - a[1])
                for b, a in zip(b_centers, a_centers)
            )

            # Extract mean object colors around centers
            b_patch_colors = []
            a_patch_colors = []
            for (bx, by), (ax, ay) in zip(b_centers, a_centers):
                bp = before[max(0, by - 4) : min(before.shape[0], by + 5), max(0, bx - 4) : min(before.shape[1], bx + 5)]
                ap = after[max(0, ay - 4) : min(after.shape[0], ay + 5), max(0, ax - 4) : min(after.shape[1], ax + 5)]
                if bp.size > 0 and ap.size > 0:
                    b_patch_colors.append(np.mean(bp, axis=(0, 1)))
                    a_patch_colors.append(np.mean(ap, axis=(0, 1)))

            max_color_dist = 0.0
            if b_patch_colors and a_patch_colors:
                max_color_dist = max(
                    float(np.linalg.norm(bc - ac) / 441.67)
                    for bc, ac in zip(b_patch_colors, a_patch_colors)
                )

            if max_color_dist > 0.18:
                # Pill color changed (unrelated object or wrong medication item)
                composite = max(composite, 0.25)
                is_shift_only = False
                is_lighting_shift = False
            elif max_dist > 3.0 and color_corr > 0.80:
                is_shift_only = True
            elif max_dist <= 3.0 and structural_dissimilarity < 0.10:
                is_lighting_shift = True
                composite = min(composite, 0.03)
        else:
            # Neither has objects: check for lighting shift
            if structural_dissimilarity < 0.12 and color_corr > 0.80:
                is_lighting_shift = True
                composite = min(composite, 0.03)

        # composite_diff is a dissimilarity fraction: 0.0 = identical, 1.0 = maximally different.
        # A non-finite score is treated as maximally different rather than silently passing.
        composite = float(np.clip(composite, 0.0, 1.0)) if np.isfinite(composite) else 1.0

        return ComparisonMetrics(
            composite_diff=round(composite, 4),
            pixel_diff_fraction=round(pixel_diff, 4),
            structural_dissimilarity=round(structural_dissimilarity, 4),
            color_correlation=round(color_corr, 4),
            contour_count=contour_count,
            total_contour_area=round(total_area, 2),
            is_lighting_shift=is_lighting_shift,
            is_shift_only=is_shift_only,
        )
