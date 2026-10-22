"""Dose schedule for MedVerify.

A schedule is a list of daily dose slots, each tied to one compartment:

    {"id": "dose-1a2b3c", "time": "08:00", "compartment_id": "A",
     "label": "Morning", "enabled": true, "days": [0, 1, 2, 3, 4, 5, 6]}

Times are local wall-clock time on the machine running the backend. `days` uses
Python weekday numbers (Monday = 0); omitted or empty means every day.

Stored in config/schedule.json and written atomically, so a crash mid-write can
never leave a half-written schedule that silently drops doses.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


class ScheduleError(ValueError):
    """Invalid schedule input; the message is safe to show to the user."""


@dataclass
class DoseSlot:
    time: str
    compartment_id: str
    label: str = ""
    enabled: bool = True
    days: list[int] = field(default_factory=lambda: list(ALL_DAYS))
    id: str = field(default_factory=lambda: f"dose-{uuid.uuid4().hex[:6]}")

    def occurrence_on(self, day: datetime) -> datetime:
        hour, minute = (int(p) for p in self.time.split(":"))
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_slot(data: dict[str, Any], compartments: set[str]) -> DoseSlot:
    time_str = str(data.get("time", "")).strip()
    if not TIME_RE.match(time_str):
        raise ScheduleError("time must be HH:MM in 24-hour format, e.g. 08:30")
    compartment = str(data.get("compartment_id", "")).strip().upper()
    if compartment not in compartments:
        raise ScheduleError(f"compartment_id must be one of {', '.join(sorted(compartments))}")
    days = data.get("days")
    if days in (None, []):
        days = list(ALL_DAYS)
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ScheduleError("days must be a list of weekday numbers 0 (Mon) to 6 (Sun)")
    label = str(data.get("label", "")).strip()[:60]
    slot = DoseSlot(
        time=time_str,
        compartment_id=compartment,
        label=label,
        enabled=bool(data.get("enabled", True)),
        days=sorted(set(days)),
    )
    if data.get("id"):
        slot.id = str(data["id"])[:40]
    return slot


class ScheduleStore:
    def __init__(self, path: Path | str, compartments: set[str]) -> None:
        self.path = Path(path)
        self.compartments = set(compartments)
        self._lock = threading.Lock()
        self.slots: list[DoseSlot] = []
        self.load_error: str | None = None
        self.load()

    def load(self) -> None:
        self.slots = []
        self.load_error = None
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data.get("doses", []):
                self.slots.append(validate_slot(raw, self.compartments))
        except (json.JSONDecodeError, OSError, ScheduleError, AttributeError) as exc:
            # Surface the problem instead of silently running with no doses.
            self.load_error = f"Could not load {self.path.name}: {exc}"
            print(f"[SCHEDULE] {self.load_error}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "doses": [s.to_dict() for s in self.slots]}
        with tempfile.NamedTemporaryFile(
            dir=self.path.parent, suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2)
        tmp_path.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in sorted(self.slots, key=lambda s: (s.time, s.compartment_id))]

    def get(self, slot_id: str) -> DoseSlot | None:
        with self._lock:
            return next((s for s in self.slots if s.id == slot_id), None)

    def add(self, data: dict[str, Any]) -> DoseSlot:
        slot = validate_slot({k: v for k, v in data.items() if k != "id"}, self.compartments)
        with self._lock:
            self.slots.append(slot)
            self._save()
        return slot

    def update(self, slot_id: str, data: dict[str, Any]) -> DoseSlot:
        with self._lock:
            existing = next((s for s in self.slots if s.id == slot_id), None)
            if existing is None:
                raise KeyError(slot_id)
            merged = {**existing.to_dict(), **data, "id": slot_id}
            slot = validate_slot(merged, self.compartments)
            self.slots[self.slots.index(existing)] = slot
            self._save()
        return slot

    def delete(self, slot_id: str) -> bool:
        with self._lock:
            before = len(self.slots)
            self.slots = [s for s in self.slots if s.id != slot_id]
            if len(self.slots) == before:
                return False
            self._save()
            return True

    def due(self, now: datetime, window: timedelta) -> list[tuple[DoseSlot, datetime]]:
        """Enabled slots whose occurrence today satisfies occurrence <= now < occurrence + window."""
        result = []
        with self._lock:
            for slot in self.slots:
                if not slot.enabled or now.weekday() not in slot.days:
                    continue
                at = slot.occurrence_on(now)
                if at <= now < at + window:
                    result.append((slot, at))
        return result

    def next_occurrences(self, now: datetime, limit: int = 5) -> list[dict[str, Any]]:
        """Upcoming dose times (within 7 days), for display."""
        upcoming = []
        with self._lock:
            for slot in self.slots:
                if not slot.enabled:
                    continue
                for offset in range(8):
                    day = now + timedelta(days=offset)
                    at = slot.occurrence_on(day)
                    if day.weekday() in slot.days and at > now:
                        upcoming.append({"slot_id": slot.id, "compartment_id": slot.compartment_id,
                                         "label": slot.label, "at": at.isoformat(timespec="minutes")})
                        break
        return sorted(upcoming, key=lambda u: u["at"])[:limit]
