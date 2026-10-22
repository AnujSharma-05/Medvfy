"""Reference State Storage for MedVerify.

Maintains separate, explicit records for:
- Expected medication references (calibrated baseline with medicine present)
- Empty compartment references (calibrated baseline with empty compartment)
- Associated camera and resolution metadata

CRITICAL INVARIANT:
Visual comparison operations MUST NEVER mutate or overwrite the expected references.
Only explicit calibration operations (save_expected, save_empty) can update references.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


class ReferenceStore:
    """Manages immutable reference baselines and camera metadata."""

    def __init__(
        self,
        baseline_file: Path | str,
        metadata_file: Path | str | None = None,
    ) -> None:
        self.baseline_path = Path(baseline_file)
        self.metadata_path = (
            Path(metadata_file)
            if metadata_file is not None
            else self.baseline_path.with_suffix(".json")
        )
        self.expected: dict[str, np.ndarray] = {}
        self.empty: dict[str, np.ndarray] = {}
        self.metadata: dict[str, Any] = {}
        self.load()

    def load(self) -> bool:
        """Load references and metadata from disk safely."""
        self.expected.clear()
        self.empty.clear()
        self.metadata.clear()

        if not self.baseline_path.exists():
            return False

        try:
            with np.load(self.baseline_path, allow_pickle=False) as data:
                for key in data.files:
                    if key.startswith("empty_"):
                        slot = key.replace("empty_", "")
                        self.empty[slot] = data[key].copy()
                    elif not key.startswith("_"):
                        self.expected[key] = data[key].copy()

            if self.metadata_path.exists():
                try:
                    self.metadata = json.loads(
                        self.metadata_path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    self.metadata = {}

            return bool(self.expected)
        except (ValueError, OSError, KeyError):
            self.expected.clear()
            self.empty.clear()
            self.metadata.clear()
            return False

    def save_expected(
        self,
        crops: dict[str, np.ndarray],
        camera_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save expected medication baseline crops without mutating empty references."""
        self.expected = {
            slot: crop.copy() for slot, crop in crops.items() if crop is not None
        }
        self._persist(camera_metadata)

    def save_empty(
        self,
        crops: dict[str, np.ndarray],
        camera_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save empty compartment baseline crops without mutating expected references."""
        self.empty = {
            slot: crop.copy() for slot, crop in crops.items() if crop is not None
        }
        self._persist(camera_metadata)

    def _persist(self, extra_meta: dict[str, Any] | None = None) -> None:
        """Persist current references and metadata atomically."""
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)

        arrays_to_save: dict[str, np.ndarray] = {}
        for slot, arr in self.expected.items():
            arrays_to_save[slot] = arr
        for slot, arr in self.empty.items():
            arrays_to_save[f"empty_{slot}"] = arr

        if not arrays_to_save:
            return

        # Atomically write npz
        dir_name = self.baseline_path.parent
        with tempfile.NamedTemporaryFile(
            dir=dir_name, suffix=".npz", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            np.savez_compressed(tmp_path, **arrays_to_save)
            tmp_path.replace(self.baseline_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        # Update metadata
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.metadata.update({
            "version": 2,
            "updated_at": timestamp,
            "expected_slots": list(self.expected.keys()),
            "empty_slots": list(self.empty.keys()),
        })
        if extra_meta:
            self.metadata.update(extra_meta)

        # Write metadata atomically
        with tempfile.NamedTemporaryFile(
            dir=dir_name, suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp_meta:
            tmp_meta_path = Path(tmp_meta.name)
            json.dump(self.metadata, tmp_meta, indent=2)

        tmp_meta_path.replace(self.metadata_path)

    def get_expected(self, slot: str) -> np.ndarray | None:
        arr = self.expected.get(slot)
        return arr.copy() if arr is not None else None

    def get_empty(self, slot: str) -> np.ndarray | None:
        arr = self.empty.get(slot)
        return arr.copy() if arr is not None else None

    def has_expected(self, slots: list[str]) -> bool:
        return all(slot in self.expected for slot in slots)

    def has_empty(self, slots: list[str]) -> bool:
        return all(slot in self.empty for slot in slots)

    def is_compatible(self, camera_width: int, camera_height: int) -> bool:
        """Validate if references are compatible with frame dimensions."""
        if not self.metadata:
            return True
        saved_w = self.metadata.get("camera_width")
        saved_h = self.metadata.get("camera_height")
        if saved_w and saved_w != camera_width:
            return False
        if saved_h and saved_h != camera_height:
            return False
        return True

    def clear(self) -> None:
        self.expected.clear()
        self.empty.clear()
        self.metadata.clear()
        if self.baseline_path.exists():
            try:
                self.baseline_path.unlink()
            except OSError:
                pass
        if self.metadata_path.exists():
            try:
                self.metadata_path.unlink()
            except OSError:
                pass
