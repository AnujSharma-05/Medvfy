"""Dose sessions: the link between the schedule, the camera, the box hardware, and notifications.

A dose session starts when a scheduled dose time arrives (or a reminder is triggered
manually). It lights the compartment's LED, sounds the buzzer, and then watches the
camera's confirmed compartment states until one of these outcomes:

    REMOVED   The target compartment was confirmed emptied by the camera.
    PARTIAL   The target compartment changed but never looked empty before the window ended.
    MISSED    The window ended with no confirmed removal from the target compartment.
    SKIPPED   A new dose time arrived while an earlier dose for the same compartment was
              still open; the earlier session continues and this occurrence is recorded.
    CANCELLED Stopped by a person (e.g. "Stop alert" in the console).

Flags record problems that make an outcome less trustworthy or need a caregiver's eye:
    WRONG_COMPARTMENT:<X>  Another compartment was opened during the session.
    NOT_LOADED             The target compartment already looked empty when the dose was due.
    CAMERA_OFFLINE         The camera process was not reporting during the session.
    DEVICE_ERROR           The LED/buzzer controller could not be reached.
    PUT_BACK               Medicine reappeared in the compartment shortly after removal.

The camera is a sensor only. This module acts on *confirmed* compartment states
(never on candidates or UNCERTAIN) and on changes of category (loaded -> removed),
so a duplicate report or a periodic heartbeat can never close a session twice or
raise the same alert again.

SAFETY BOUNDARY:
REMOVED means the camera saw the compartment emptied. It does not prove the
medicine was swallowed, and messages never say it was.
"""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from backend.notifier import CRITICAL, INFO, WARNING, Notifier
from backend.schedule import ScheduleStore

# Compartment state categories (states come from vision/state_machine.py)
LOADED_STATES = {"EXPECTED_PRESENT", "OBJECT_RETURNED"}
REMOVED_STATES = {"REMOVAL_DETECTED", "EMPTY"}
CHANGED_STATES = {"PARTIAL_OR_WRONG"}
CONFIRMED_STATES = LOADED_STATES | REMOVED_STATES | CHANGED_STATES

ACTIVE = "ACTIVE"
CLOSED = "CLOSED"


def category(state: str | None) -> str:
    if state in LOADED_STATES:
        return "loaded"
    if state in REMOVED_STATES:
        return "removed"
    if state in CHANGED_STATES:
        return "changed"
    return "unknown"


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


@dataclass
class DoseConfig:
    window_minutes: float = 30.0          # How long a dose stays open before it is MISSED
    remind_every_minutes: float = 5.0     # Re-alert interval while a dose is open
    camera_timeout_seconds: float = 30.0  # No heartbeat for this long = camera offline
    put_back_grace_minutes: float = 10.0  # A return this soon after removal is suspicious

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "DoseConfig":
        d = cls()
        cfg = settings.get("doses") or {}
        return cls(
            window_minutes=float(cfg.get("window_minutes", d.window_minutes)),
            remind_every_minutes=float(cfg.get("remind_every_minutes", d.remind_every_minutes)),
            camera_timeout_seconds=float(cfg.get("camera_timeout_seconds", d.camera_timeout_seconds)),
            put_back_grace_minutes=float(cfg.get("put_back_grace_minutes", d.put_back_grace_minutes)),
        )


@dataclass
class DoseSession:
    compartment_id: str
    started_at: str
    deadline: str
    label: str = ""
    schedule_id: str | None = None
    scheduled_for: str | None = None
    occurrence_key: str | None = None
    status: str = ACTIVE
    outcome: str | None = None
    closed_at: str | None = None
    reminders_sent: int = 0
    last_reminder_at: str | None = None
    silenced_at: str | None = None   # Patient pressed "Stop sound": no more beeps, dose stays open
    closed_by: str | None = None     # "camera", "timeout", "caretaker", ...
    flags: list[str] = field(default_factory=list)
    events: list[dict[str, str]] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"dose-{uuid.uuid4().hex[:10]}")

    def add_flag(self, flag: str) -> bool:
        if flag in self.flags:
            return False
        self.flags.append(flag)
        return True

    def log(self, now: datetime, text: str) -> None:
        self.events.append({"at": now.isoformat(timespec="seconds"), "text": text})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DoseManager:
    def __init__(
        self,
        device: Any,
        notifier: Notifier,
        schedule: ScheduleStore,
        compartments: list[str],
        sessions_file: Path | str | None = None,
        config: DoseConfig | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.device = device
        self.notifier = notifier
        self.schedule = schedule
        self.compartments = list(compartments)
        self.sessions_file = Path(sessions_file) if sessions_file else None
        self.config = config or DoseConfig()
        self.clock = clock
        self._lock = threading.RLock()

        self.sessions: list[DoseSession] = []
        self.compartment_states: dict[str, dict[str, Any]] = {
            c: {"state": "UNKNOWN", "updated_at": None} for c in self.compartments
        }
        self.camera: dict[str, Any] = {"last_seen": None, "monitoring": False, "online": False}
        self._camera_alerted_offline = False
        self._load_sessions()

    # ------------------------------------------------------------------ helpers
    def _now(self, now: datetime | None) -> datetime:
        return now if now is not None else self.clock()

    def active_sessions(self) -> list[DoseSession]:
        return [s for s in self.sessions if s.status == ACTIVE]

    def _active_for(self, compartment: str) -> DoseSession | None:
        return next((s for s in self.active_sessions() if s.compartment_id == compartment), None)

    def camera_online(self, now: datetime | None = None) -> bool:
        now = self._now(now)
        last = self.camera.get("last_seen")
        if last is None:
            return False
        return (now - datetime.fromisoformat(last)).total_seconds() <= self.config.camera_timeout_seconds

    def _notify(self, session: DoseSession | None, title: str, message: str, level: str, kind: str,
                compartment: str | None = None) -> None:
        self.notifier.notify(
            title, message, level=level, kind=kind,
            compartment_id=compartment or (session.compartment_id if session else None),
            session_id=session.id if session else None,
        )

    def _device_remind(self, session: DoseSession, now: datetime) -> None:
        try:
            self.device.trigger_reminder(session.compartment_id, session.id)
        except Exception as exc:
            session.log(now, f"Device error: {exc}")
            if session.add_flag("DEVICE_ERROR"):
                self._notify(session, "Pill box lights/buzzer not responding",
                             f"Could not signal compartment {session.compartment_id}: {exc}. "
                             "Reminders continue here and on your phone.", WARNING, "DEVICE_ERROR")

    def _refresh_device(self, now: datetime) -> None:
        """Point the hardware at the most recent open dose, or switch it off."""
        active = self.active_sessions()
        if active:
            self._device_remind(active[-1], now)
            return
        try:
            self.device.stop_alert()
        except Exception as exc:
            print(f"[DOSE] Device stop failed: {exc}")

    # --------------------------------------------------------------- sessions
    def start_session(
        self,
        compartment: str,
        now: datetime | None = None,
        label: str = "",
        schedule_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> DoseSession:
        with self._lock:
            now = self._now(now)
            compartment = compartment.upper()
            base = scheduled_for or now
            occurrence_key = f"{schedule_id}@{base.isoformat(timespec='minutes')}" if schedule_id else None

            existing = self._active_for(compartment)
            if existing is not None:
                skipped = DoseSession(
                    compartment_id=compartment, started_at=now.isoformat(timespec="seconds"),
                    deadline=now.isoformat(timespec="seconds"), label=label, schedule_id=schedule_id,
                    scheduled_for=base.isoformat(timespec="minutes"), occurrence_key=occurrence_key,
                    status=CLOSED, outcome="SKIPPED", closed_at=now.isoformat(timespec="seconds"),
                )
                skipped.log(now, f"Dose for {compartment} already open ({existing.id}); not started twice.")
                self.sessions.append(skipped)
                self._save()
                return skipped

            session = DoseSession(
                compartment_id=compartment,
                started_at=now.isoformat(timespec="seconds"),
                deadline=(base + timedelta(minutes=self.config.window_minutes)).isoformat(timespec="seconds"),
                label=label,
                schedule_id=schedule_id,
                scheduled_for=base.isoformat(timespec="minutes") if scheduled_for else None,
                occurrence_key=occurrence_key,
                reminders_sent=1,
                last_reminder_at=now.isoformat(timespec="seconds"),
            )
            session.log(now, "Reminder started" + (" (manual)" if not schedule_id else ""))
            self.sessions.append(session)

            name = f"{label} " if label else ""
            self._notify(session, f"Time for your {name}medicine",
                         f"Please take the medicine from compartment {compartment} (light is on).",
                         INFO, "DOSE_DUE")
            self._device_remind(session, now)

            if not self.camera_online(now):
                session.add_flag("CAMERA_OFFLINE")
                self._camera_alerted_offline = True  # This message covers the periodic camera check too
                self._notify(session, "Camera not monitoring",
                             f"Dose for compartment {compartment} is due but the camera is not reporting, "
                             "so it cannot be checked automatically.", WARNING, "CAMERA_OFFLINE")
            elif self.camera.get("trial"):
                session.add_flag("CAMERA_IN_TRIAL")
                self._notify(session, "Camera busy with test trials",
                             f"Dose for compartment {compartment} is due, but the camera is in trial mode "
                             "and is not checking doses.", WARNING, "CAMERA_IN_TRIAL")
            state = self.compartment_states.get(compartment, {}).get("state")
            if category(state) == "removed":
                session.add_flag("NOT_LOADED")
                self._notify(session, f"Compartment {compartment} looks empty",
                             f"The dose in compartment {compartment} is due, but the camera already sees it "
                             "empty. It may need refilling.", WARNING, "NOT_LOADED")
            self._save()
            return session

    def close_session(self, session: DoseSession, outcome: str, now: datetime, note: str = "") -> None:
        session.status = CLOSED
        session.outcome = outcome
        session.closed_at = now.isoformat(timespec="seconds")
        session.log(now, note or f"Closed: {outcome}")
        self._refresh_device(now)
        self._save()

    def find(self, session_id: str) -> DoseSession | None:
        return next((s for s in self.sessions if s.id == session_id), None)

    def silence(self, now: datetime | None = None, by: str = "patient") -> int:
        """Stop the sound for open doses WITHOUT closing them (the light and the dose stay)."""
        with self._lock:
            now = self._now(now)
            silenced = 0
            for s in self.active_sessions():
                if not s.silenced_at:
                    s.silenced_at = now.isoformat(timespec="seconds")
                    s.log(now, f"Sound stopped by {by}")
                    silenced += 1
            if silenced:
                quiet = getattr(self.device, "silence", None)
                try:
                    if quiet:
                        quiet()
                except Exception as exc:
                    print(f"[DOSE] Device silence failed: {exc}")
                self._save()
            return silenced

    def remind_now(self, compartment: str, now: datetime | None = None, by: str = "caretaker") -> DoseSession:
        """Beep again for an open dose, or open a new dose for this compartment."""
        with self._lock:
            now = self._now(now)
            compartment = compartment.upper()
            own = self._active_for(compartment)
            if own is None:
                return self.start_session(compartment, now, label=f"Reminder from {by}")
            own.reminders_sent += 1
            own.last_reminder_at = now.isoformat(timespec="seconds")
            own.silenced_at = None  # An explicit reminder overrides an earlier "stop sound"
            own.log(now, f"Reminder sent by {by}")
            self._device_remind(own, now)
            self._notify(own, f"Reminder sent: compartment {compartment}",
                         f"The box was asked to beep again for compartment {compartment}.", INFO, "REMOTE_REMINDER")
            self._save()
            return own

    def cancel_session(self, session_id: str, now: datetime | None = None, by: str = "caretaker") -> DoseSession | None:
        with self._lock:
            now = self._now(now)
            s = self.find(session_id)
            if s is None or s.status != ACTIVE:
                return None
            s.closed_by = by
            self.close_session(s, "CANCELLED", now, f"Cancelled by {by}")
            return s

    def confirm_session(self, session_id: str, now: datetime | None = None, by: str = "caretaker") -> DoseSession | None:
        """A person confirms the dose (e.g. by phone call). Recorded as CONFIRMED, distinct from the
        camera's REMOVED, so reports always show which evidence a result rests on."""
        with self._lock:
            now = self._now(now)
            s = self.find(session_id)
            if s is None or s.status != ACTIVE:
                return None
            s.closed_by = by
            self.close_session(s, "CONFIRMED", now, f"Confirmed by {by} (not seen by the camera)")
            self._notify(s, f"Dose confirmed by {by}: compartment {s.compartment_id}",
                         f"Marked as taken by {by} at {fmt_time(now)}. The camera did not confirm a removal.",
                         INFO, "DOSE_CONFIRMED")
            return s

    def mark_recalibrated(self, now: datetime | None = None, by: str = "caretaker") -> None:
        """The camera re-photographed the box as 'loaded' (refill). Reset states without treating
        the change as a return, so a refill soon after a dose is not reported as 'put back'."""
        with self._lock:
            now = self._now(now)
            for c in self.compartment_states:
                self.compartment_states[c] = {"state": "EXPECTED_PRESENT",
                                              "updated_at": now.isoformat(timespec="seconds"), "pending": None}
            self._notify(None, "Box recalibrated",
                         f"Requested by {by}: the camera saved the current box as 'loaded' for every compartment.",
                         INFO, "RECALIBRATED")
            self._save()

    def cancel_all(self, now: datetime | None = None, reason: str = "Stopped from console") -> int:
        with self._lock:
            now = self._now(now)
            active = self.active_sessions()
            for s in active:
                s.status = CLOSED
                s.outcome = "CANCELLED"
                s.closed_at = now.isoformat(timespec="seconds")
                s.log(now, reason)
            self._refresh_device(now)
            self._save()
            return len(active)

    # ------------------------------------------------------------------- tick
    def tick(self, now: datetime | None = None) -> None:
        """Advance time: start due doses, re-remind, time out, and watch camera health."""
        with self._lock:
            now = self._now(now)
            window = timedelta(minutes=self.config.window_minutes)
            known = {s.occurrence_key for s in self.sessions if s.occurrence_key}
            for slot, at in self.schedule.due(now, window):
                key = f"{slot.id}@{at.isoformat(timespec='minutes')}"
                if key not in known:
                    self.start_session(slot.compartment_id, now, slot.label, slot.id, at)

            for session in list(self.active_sessions()):
                if now >= datetime.fromisoformat(session.deadline):
                    self._expire(session, now)
                    continue
                last = datetime.fromisoformat(session.last_reminder_at or session.started_at)
                if now - last >= timedelta(minutes=self.config.remind_every_minutes):
                    session.reminders_sent += 1
                    session.last_reminder_at = now.isoformat(timespec="seconds")
                    session.log(now, f"Reminder #{session.reminders_sent}"
                                     + (" (sound silenced by patient)" if session.silenced_at else ""))
                    if not session.silenced_at:
                        self._device_remind(session, now)
                    self._notify(session, "Reminder: medicine still waiting",
                                 f"Compartment {session.compartment_id} has not been opened yet "
                                 f"(reminder {session.reminders_sent}"
                                 f"{'; sound silenced at the box' if session.silenced_at else ''}).",
                                 WARNING, "DOSE_REMINDER")
                    self._save()

            self._check_camera(now)

    def _expire(self, session: DoseSession, now: datetime) -> None:
        partial = "PARTIAL" in session.flags
        outcome = "PARTIAL" if partial else "MISSED"
        extra = ""
        if "CAMERA_OFFLINE" in session.flags:
            extra = " The camera was not reporting, so this could not be checked automatically."
        elif "NOT_LOADED" in session.flags:
            extra = " The compartment already looked empty when the dose was due."
        title = (f"Dose possibly incomplete: compartment {session.compartment_id}" if partial
                 else f"Missed dose: compartment {session.compartment_id}")
        message = (f"Compartment {session.compartment_id} was due at "
                   f"{session.scheduled_for[-5:] if session.scheduled_for else session.started_at[11:16]} "
                   f"and was {'only partly changed' if partial else 'not emptied'} within "
                   f"{int(self.config.window_minutes)} minutes.{extra}")
        self._notify(session, title, message, CRITICAL, f"DOSE_{outcome}")
        session.closed_by = "timeout"
        self.close_session(session, outcome, now, f"Window ended: {outcome}")

    def _check_camera(self, now: datetime) -> None:
        online = self.camera_online(now)
        self.camera["online"] = online
        active = self.active_sessions()
        if not online and active and not self._camera_alerted_offline:
            self._camera_alerted_offline = True
            for s in active:
                s.add_flag("CAMERA_OFFLINE")
            self._notify(None, "Camera stopped monitoring",
                         "A dose is open but the camera has stopped reporting. Check that the MedVerify "
                         "camera window is running.", WARNING, "CAMERA_OFFLINE")
            self._save()
        elif online and self._camera_alerted_offline:
            self._camera_alerted_offline = False
            self._notify(None, "Camera monitoring restored", "The camera is reporting again.", INFO, "CAMERA_ONLINE")

    # ------------------------------------------------------------- vision input
    def heartbeat(self, payload: dict[str, Any], now: datetime | None = None) -> None:
        """Camera liveness plus a snapshot of states; reconciles any missed observation."""
        with self._lock:
            now = self._now(now)
            self.camera["last_seen"] = now.isoformat(timespec="seconds")
            self.camera["monitoring"] = bool(payload.get("monitoring", False))
            self.camera["trial"] = bool(payload.get("trial", False))
            self.camera["camera_index"] = payload.get("camera_index")
            self.camera["online"] = True
            for compartment, state in (payload.get("states") or {}).items():
                self.record_state(str(compartment).upper(), str(state), now, source="heartbeat")
            self._check_camera(now)

    def record_state(self, compartment: str, state: str, now: datetime | None = None,
                     source: str = "observation") -> None:
        """Apply one compartment state report from the camera."""
        with self._lock:
            now = self._now(now)
            if compartment not in self.compartment_states:
                return
            entry = self.compartment_states[compartment]
            if state not in CONFIRMED_STATES:
                # Candidates and UNCERTAIN are shown but never acted on.
                entry["pending"] = state
                return
            previous = entry.get("state")
            entry.update({"state": state, "updated_at": now.isoformat(timespec="seconds"), "pending": None})
            before, after = category(previous), category(state)
            if before == after:
                return
            self._on_category_change(compartment, before, after, now, source)
            self._save()

    def _on_category_change(self, compartment: str, before: str, after: str, now: datetime, source: str) -> None:
        own = self._active_for(compartment)
        others = [s for s in self.active_sessions() if s.compartment_id != compartment]

        if own is not None:
            if after == "removed":
                own.log(now, f"Camera confirmed compartment {compartment} emptied ({source})")
                own.closed_by = "camera"
                self._notify(own, f"Medicine removed from compartment {compartment}",
                             f"Compartment {compartment} was emptied at {fmt_time(now)}. "
                             "(The camera saw it removed; it cannot see it being swallowed.)",
                             INFO, "DOSE_REMOVED")
                self.close_session(own, "REMOVED", now, "Removal confirmed by camera")
            elif after == "changed":
                own.log(now, "Compartment changed but does not look empty")
                if own.add_flag("PARTIAL"):
                    self._notify(own, f"Compartment {compartment} only partly emptied?",
                                 f"Compartment {compartment} changed but does not look empty. "
                                 "Some medicine may still be inside.", WARNING, "DOSE_PARTIAL")
            elif after == "loaded" and before == "changed":
                own.log(now, "Compartment looks full again")
            return

        if others and after in ("removed", "changed"):
            for s in others:
                s.add_flag(f"WRONG_COMPARTMENT:{compartment}")
                s.log(now, f"Compartment {compartment} opened instead")
                # He is at the box doing the wrong thing: beep again even if he silenced it
                # earlier (re-pointing the light beeps on this firmware anyway), and say so.
                s.silenced_at = None
            targets = ", ".join(sorted({s.compartment_id for s in others}))
            self._notify(others[-1], f"Wrong compartment: {compartment}",
                         f"Compartment {compartment} was opened, but the medicine due now is in "
                         f"compartment {targets}.", WARNING, "WRONG_COMPARTMENT", compartment=compartment)
            self._refresh_device(now)  # Re-point the light at the right compartment
            return

        if after == "loaded" and before == "removed":
            recent = self._recent_removed(compartment, now)
            if recent is not None:
                recent.add_flag("PUT_BACK")
                recent.log(now, "Medicine reappeared shortly after removal")
                self._notify(recent, f"Medicine put back in compartment {compartment}?",
                             f"Compartment {compartment} looks full again {int((now - datetime.fromisoformat(recent.closed_at)).total_seconds() // 60)} "
                             "minute(s) after it was emptied. The dose may not have been taken.",
                             WARNING, "PUT_BACK")
            else:
                self._notify(None, f"Compartment {compartment} refilled",
                             f"Compartment {compartment} looks loaded again.", INFO, "REFILLED",
                             compartment=compartment)
            return

        if before == "loaded" and after in ("removed", "changed"):
            what = "emptied" if after == "removed" else "changed"
            self._notify(None, f"Compartment {compartment} {what} outside dose time",
                         f"Compartment {compartment} was {what} at {fmt_time(now)}, but no dose was due "
                         "from it.", WARNING, "UNSCHEDULED_REMOVAL", compartment=compartment)

    def _recent_removed(self, compartment: str, now: datetime) -> DoseSession | None:
        grace = timedelta(minutes=self.config.put_back_grace_minutes)
        for s in reversed(self.sessions):
            if s.compartment_id == compartment and s.outcome == "REMOVED" and s.closed_at:
                return s if now - datetime.fromisoformat(s.closed_at) <= grace else None
        return None

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if not self.sessions_file:
            return
        self.sessions = self.sessions[-300:]
        self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "sessions": [s.to_dict() for s in self.sessions]}
        with tempfile.NamedTemporaryFile(
            dir=self.sessions_file.parent, suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2)
        tmp_path.replace(self.sessions_file)

    def _load_sessions(self) -> None:
        if not self.sessions_file or not self.sessions_file.exists():
            return
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            self.sessions = [DoseSession(**raw) for raw in data.get("sessions", [])]
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            print(f"[DOSE] Could not load sessions ({exc}); starting with an empty history.")
            self.sessions = []

    # ------------------------------------------------------------------ status
    def status(self, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            now = self._now(now)
            closed = [s for s in self.sessions if s.status == CLOSED]
            counts: dict[str, int] = {}
            for s in closed:
                counts[s.outcome or "UNKNOWN"] = counts.get(s.outcome or "UNKNOWN", 0) + 1
            return {
                "now": now.isoformat(timespec="seconds"),
                "camera": {**self.camera, "online": self.camera_online(now)},
                "compartments": self.compartment_states,
                "active_sessions": [s.to_dict() for s in self.active_sessions()],
                "recent_sessions": [s.to_dict() for s in self.sessions[-20:]][::-1],
                "upcoming": self.schedule.next_occurrences(now),
                "outcome_counts": counts,
                "config": asdict(self.config),
            }
