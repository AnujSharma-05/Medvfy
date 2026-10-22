"""State Machine and Event Semantics for MedVerify Compartments.

Implements explicit state-transition logic, temporal confirmation (debouncing),
duplicate event suppression, state persistence across process restarts,
and conservative uncertainty handling.

SAFETY BOUNDARY:
Visual state transitions represent visible appearance changes only.
They NEVER imply dose ingestion.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
import numpy as np


class CompartmentState(str, Enum):
    """Explicit compartment observation states."""
    UNKNOWN = "UNKNOWN"
    EMPTY = "EMPTY"
    EXPECTED_PRESENT = "EXPECTED_PRESENT"
    REMOVAL_CANDIDATE = "REMOVAL_CANDIDATE"
    REMOVAL_DETECTED = "REMOVAL_DETECTED"
    RETURN_CANDIDATE = "RETURN_CANDIDATE"
    OBJECT_RETURNED = "OBJECT_RETURNED"
    PARTIAL_OR_WRONG = "PARTIAL_OR_WRONG"
    UNCERTAIN = "UNCERTAIN"
    CAMERA_ERROR = "CAMERA_ERROR"


@dataclass
class StateTransition:
    slot: str
    previous_state: CompartmentState
    new_state: CompartmentState
    timestamp_utc: str
    is_new_event: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


class CompartmentStateMachine:
    """Manages state transitions, candidate debouncing, duplicate suppression,
    and durable state persistence for compartments."""

    def __init__(
        self,
        debounce_frames: int = 1,
        state_file: Path | str | None = None,
        confirm_seconds: float = 0.0,
    ) -> None:
        """
        Args:
            debounce_frames: Minimum consecutive consistent observations to confirm
                a removal or return.
            state_file: Optional path for persisting confirmed states.
            confirm_seconds: Minimum time a removal/return candidate must persist,
                uninterrupted, before it is confirmed. Both this and debounce_frames
                must be satisfied. Counting observations alone is not enough for
                continuous monitoring: consecutive frames at camera rate are only
                tens of milliseconds apart and share the same transient disturbance.
        """
        self.debounce_frames = max(1, debounce_frames)
        self.confirm_seconds = max(0.0, float(confirm_seconds))
        self.state_file = Path(state_file) if state_file else None
        self.current_states: dict[str, CompartmentState] = {}
        self.candidate_states: dict[str, CompartmentState] = {}
        self.candidate_counts: dict[str, int] = {}
        self.candidate_started: dict[str, float] = {}
        self.last_confirmed_states: dict[str, CompartmentState] = {}
        self.history: dict[str, list[StateTransition]] = {}

        if self.state_file:
            self.load_state()

    @property
    def _confirms_immediately(self) -> bool:
        """True when a single observation may confirm removal/return (no candidate stage)."""
        return self.debounce_frames == 1 and self.confirm_seconds == 0.0

    def _removal_target(self) -> CompartmentState:
        """Removal goes through the candidate stage unless single-observation confirmation is configured."""
        return (
            CompartmentState.REMOVAL_DETECTED
            if self._confirms_immediately
            else CompartmentState.REMOVAL_CANDIDATE
        )

    def reset_candidate(self, slot: str) -> None:
        """Interrupt a pending candidate so its confirmation count and clock restart.

        Used when evidence is unavailable for a while (e.g. the scene is moving): the
        unobserved period must not count toward confirming a transition.
        """
        self.candidate_counts[slot] = 0

    def get_state(self, slot: str) -> CompartmentState:
        return self.current_states.get(slot, CompartmentState.UNKNOWN)

    def set_initial_state(self, slot: str, state: CompartmentState) -> None:
        self.current_states[slot] = state
        self.last_confirmed_states[slot] = state
        self.candidate_states[slot] = state
        self.candidate_counts[slot] = 0
        if self.state_file:
            self.save_state()

    def evaluate(
        self,
        slot: str,
        diff_from_expected: float,
        diff_from_empty: float | None = None,
        is_blurry: bool = False,
        is_occluded: bool = False,
        change_threshold: float = 0.08,
        match_threshold: float = 0.05,
        extra_metrics: dict[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> StateTransition:
        """Evaluate visual evidence and transition the compartment state.

        observed_at is a monotonic timestamp (seconds) for the observation; it defaults
        to time.monotonic() and is used only for time-based candidate confirmation.
        """
        now = time.monotonic() if observed_at is None else float(observed_at)
        current = self.get_state(slot)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        metrics = {
            "diff_from_expected": (
                round(float(diff_from_expected), 4)
                if diff_from_expected is not None and not np.isnan(diff_from_expected)
                else None
            ),
            "diff_from_empty": (
                round(float(diff_from_empty), 4)
                if diff_from_empty is not None and not np.isnan(diff_from_empty)
                else None
            ),
            **(extra_metrics or {}),
        }

        # 1. Metric validity check: invalid metrics yield UNCERTAIN
        if (
            diff_from_expected is None
            or not isinstance(diff_from_expected, (int, float))
            or np.isnan(diff_from_expected)
            or diff_from_expected < 0.0
        ):
            target_state = CompartmentState.UNCERTAIN
            reason = "Invalid or corrupted visual difference metric"
            # Reset candidate tracking on invalid frame
            self.candidate_counts[slot] = 0
            return self._apply_transition(
                slot, current, target_state, timestamp, reason, metrics, confidence=0.0
            )

        # 2. Quality / blur / occlusion checks
        if is_blurry or is_occluded:
            target_state = CompartmentState.UNCERTAIN
            reason = "Frame blurred or occluded; decision deferred"
            # Reset candidate count on interrupted sequence
            self.candidate_counts[slot] = 0
            return self._apply_transition(
                slot, current, target_state, timestamp, reason, metrics, confidence=0.0
            )

        # 3. Determine target state candidate from visual differences
        matches_expected = diff_from_expected < match_threshold
        changed_from_expected = diff_from_expected >= change_threshold

        matches_empty = False
        if diff_from_empty is not None and not np.isnan(diff_from_empty):
            matches_empty = diff_from_empty < match_threshold

        # Transition rules based on current confirmed state:
        if current in (CompartmentState.UNKNOWN, CompartmentState.EXPECTED_PRESENT):
            if matches_expected:
                target_state = CompartmentState.EXPECTED_PRESENT
                reason = "Visible contents match expected baseline"
            elif changed_from_expected:
                if matches_empty:
                    target_state = (
                        CompartmentState.REMOVAL_DETECTED
                        if self._confirms_immediately
                        else CompartmentState.REMOVAL_CANDIDATE
                    )
                    reason = "Expected medication removed (compartment appears empty)"
                elif diff_from_empty is not None and diff_from_empty >= change_threshold:
                    target_state = CompartmentState.PARTIAL_OR_WRONG
                    reason = "Content changed but does not match empty baseline (unexpected object or partial)"
                else:
                    target_state = (
                        CompartmentState.REMOVAL_DETECTED
                        if self._confirms_immediately
                        else CompartmentState.REMOVAL_CANDIDATE
                    )
                    reason = "Significant difference from expected medication baseline"
            else:
                target_state = CompartmentState.UNCERTAIN
                reason = "Difference lies in marginal threshold band"

        elif current == CompartmentState.REMOVAL_CANDIDATE:
            # Candidate was never confirmed. If observation matches expected, revert to EXPECTED_PRESENT
            if matches_expected:
                target_state = CompartmentState.EXPECTED_PRESENT
                reason = "Transient disturbance cleared; matches expected baseline"
            elif matches_empty or (diff_from_empty is not None and diff_from_empty < change_threshold):
                target_state = CompartmentState.REMOVAL_CANDIDATE
                reason = "Medication removal candidate persists"
            elif diff_from_empty is not None and diff_from_empty >= change_threshold:
                target_state = CompartmentState.PARTIAL_OR_WRONG
                reason = "Candidate resolved to unexpected or partial contents"
            else:
                target_state = CompartmentState.REMOVAL_CANDIDATE
                reason = "Medication removal candidate persists"

        elif current in (
            CompartmentState.REMOVAL_DETECTED,
            CompartmentState.EMPTY,
        ):
            if matches_expected:
                target_state = (
                    CompartmentState.OBJECT_RETURNED
                    if self._confirms_immediately
                    else CompartmentState.RETURN_CANDIDATE
                )
                reason = "Medication returned matching expected baseline"
            elif matches_empty or (diff_from_empty is not None and diff_from_empty < change_threshold) or (diff_from_empty is None and changed_from_expected):
                # Scene remains in confirmed removed/empty state: do not create duplicate event
                target_state = current
                reason = f"Compartment remains in confirmed {current.value} state"
            elif changed_from_expected and not matches_empty:
                target_state = CompartmentState.PARTIAL_OR_WRONG
                reason = "Object present but does not match expected baseline"
            else:
                target_state = CompartmentState.UNCERTAIN
                reason = "Indeterminate state transition from removed state"

        elif current == CompartmentState.RETURN_CANDIDATE:
            if matches_expected:
                target_state = CompartmentState.RETURN_CANDIDATE
                reason = "Return candidate persists"
            elif matches_empty:
                target_state = CompartmentState.REMOVAL_DETECTED
                reason = "Reverted to empty before return was confirmed"
            else:
                target_state = CompartmentState.PARTIAL_OR_WRONG
                reason = "Candidate resolved to unexpected object"

        elif current == CompartmentState.OBJECT_RETURNED:
            if matches_expected:
                target_state = CompartmentState.OBJECT_RETURNED
                reason = "Medication remains present after return"
            elif changed_from_expected:
                target_state = (
                    CompartmentState.REMOVAL_DETECTED
                    if self._confirms_immediately
                    else CompartmentState.REMOVAL_CANDIDATE
                )
                reason = "Object removed again after previous return"
            else:
                target_state = CompartmentState.UNCERTAIN
                reason = "Marginal difference after object return"

        elif current == CompartmentState.PARTIAL_OR_WRONG:
            if matches_expected:
                target_state = CompartmentState.EXPECTED_PRESENT
                reason = "Expected medication restored"
            elif matches_empty:
                target_state = CompartmentState.EMPTY
                reason = "Compartment cleared to empty"
            else:
                target_state = CompartmentState.PARTIAL_OR_WRONG
                reason = "Compartment contents remain inconsistent with expected baseline"

        else:  # UNCERTAIN or CAMERA_ERROR
            last_confirmed = self.last_confirmed_states.get(slot, CompartmentState.UNKNOWN)
            if matches_expected:
                if last_confirmed in (CompartmentState.REMOVAL_DETECTED, CompartmentState.EMPTY):
                    target_state = (
                        CompartmentState.OBJECT_RETURNED
                        if self._confirms_immediately
                        else CompartmentState.RETURN_CANDIDATE
                    )
                    reason = "Medication returned after recovery from uncertain state"
                else:
                    target_state = CompartmentState.EXPECTED_PRESENT
                    reason = "Recovered from uncertain state: matches expected baseline"
            elif matches_empty:
                target_state = (
                    self._removal_target()
                    if last_confirmed == CompartmentState.EXPECTED_PRESENT
                    else (last_confirmed if last_confirmed != CompartmentState.UNKNOWN else CompartmentState.EMPTY)
                )
                reason = "Recovered from uncertain state: compartment confirmed empty"
            elif changed_from_expected:
                if diff_from_empty is not None and diff_from_empty >= change_threshold:
                    target_state = CompartmentState.PARTIAL_OR_WRONG
                    reason = "Recovered from uncertain state: unexpected object present"
                else:
                    target_state = (
                        self._removal_target()
                        if last_confirmed == CompartmentState.EXPECTED_PRESENT
                        else (last_confirmed if last_confirmed != CompartmentState.UNKNOWN else CompartmentState.EMPTY)
                    )
                    reason = "Recovered from uncertain state: medication removed"
            else:
                target_state = CompartmentState.UNCERTAIN
                reason = "Marginal evidence after recovery from uncertain state"

        # 4. Temporal confirmation: N consistent observations AND at least confirm_seconds elapsed
        if not self._confirms_immediately:
            if target_state in (
                CompartmentState.REMOVAL_CANDIDATE,
                CompartmentState.RETURN_CANDIDATE,
            ):
                candidate_type = (
                    CompartmentState.REMOVAL_DETECTED
                    if target_state == CompartmentState.REMOVAL_CANDIDATE
                    else CompartmentState.OBJECT_RETURNED
                )
                if self.candidate_states.get(slot) == candidate_type:
                    if self.candidate_counts.get(slot, 0) == 0:
                        # Sequence was interrupted (invalid frame, motion reset): restart the clock
                        self.candidate_started[slot] = now
                    self.candidate_counts[slot] = self.candidate_counts.get(slot, 0) + 1
                    count = self.candidate_counts[slot]
                    elapsed = now - self.candidate_started.get(slot, now)
                    if count >= self.debounce_frames and elapsed >= self.confirm_seconds:
                        target_state = candidate_type
                        reason = (
                            f"Confirmed {candidate_type.value} after {count} consistent "
                            f"observations over {elapsed:.1f}s"
                        )
                        self.candidate_counts[slot] = 0
                else:
                    self.candidate_states[slot] = candidate_type
                    self.candidate_counts[slot] = 1
                    self.candidate_started[slot] = now
            else:
                self.candidate_states[slot] = target_state
                self.candidate_counts[slot] = 0

        return self._apply_transition(
            slot, current, target_state, timestamp, reason, metrics
        )

    def _apply_transition(
        self,
        slot: str,
        previous_state: CompartmentState,
        new_state: CompartmentState,
        timestamp: str,
        reason: str,
        metrics: dict[str, Any],
        confidence: float = 1.0,
    ) -> StateTransition:
        # Duplicate event suppression: only is_new_event if state actually changed
        is_new_event = (new_state != previous_state)

        self.current_states[slot] = new_state
        if new_state not in (
            CompartmentState.REMOVAL_CANDIDATE,
            CompartmentState.RETURN_CANDIDATE,
            CompartmentState.UNCERTAIN,
        ):
            self.last_confirmed_states[slot] = new_state
            if is_new_event and self.state_file:
                self.save_state()

        transition = StateTransition(
            slot=slot,
            previous_state=previous_state,
            new_state=new_state,
            timestamp_utc=timestamp,
            is_new_event=is_new_event,
            reason=reason,
            metrics=metrics,
            confidence=confidence,
        )

        if slot not in self.history:
            self.history[slot] = []
        self.history[slot].append(transition)

        return transition

    def save_state(self, filepath: Path | str | None = None) -> None:
        """Atomically persist confirmed states to disk."""
        target = Path(filepath) if filepath else self.state_file
        if not target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "confirmed_states": {s: st.value for s, st in self.last_confirmed_states.items()},
            "current_states": {s: st.value for s, st in self.current_states.items()},
        }
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2)
        tmp_path.replace(target)

    def load_state(self, filepath: Path | str | None = None) -> bool:
        """Load confirmed states from disk."""
        target = Path(filepath) if filepath else self.state_file
        if not target or not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            confirmed = data.get("confirmed_states", {})
            current = data.get("current_states", {})
            for s, st_str in confirmed.items():
                try:
                    self.last_confirmed_states[s] = CompartmentState(st_str)
                except ValueError:
                    pass
            for s, st_str in current.items():
                try:
                    self.current_states[s] = CompartmentState(st_str)
                except ValueError:
                    pass
            return bool(self.last_confirmed_states)
        except (json.JSONDecodeError, OSError):
            return False

    def clear_state(self) -> None:
        """Clear all in-memory and on-disk state."""
        self.current_states.clear()
        self.candidate_states.clear()
        self.candidate_counts.clear()
        self.candidate_started.clear()
        self.last_confirmed_states.clear()
        self.history.clear()
        if self.state_file and self.state_file.exists():
            try:
                self.state_file.unlink()
            except OSError:
                pass

    def to_legacy_state(self, state: CompartmentState) -> str:
        """Convert state enum to backward-compatible CHANGED / NO CHANGE / UNCERTAIN string."""
        if state == CompartmentState.EXPECTED_PRESENT:
            return "NO CHANGE"
        if state in (
            CompartmentState.REMOVAL_DETECTED,
            CompartmentState.OBJECT_RETURNED,
            CompartmentState.PARTIAL_OR_WRONG,
            CompartmentState.EMPTY,
        ):
            return "CHANGED"
        if state == CompartmentState.UNCERTAIN:
            return "UNCERTAIN"
        if state in (
            CompartmentState.REMOVAL_CANDIDATE,
            CompartmentState.RETURN_CANDIDATE,
        ):
            return "PENDING"
        return "UNKNOWN"
