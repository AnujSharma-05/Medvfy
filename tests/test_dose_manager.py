"""Dose session scenarios on a simulated clock (no real waiting, no hardware, no network)."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from backend.dose_manager import ACTIVE, CLOSED, DoseConfig, DoseManager
from backend.notifier import Notifier
from backend.schedule import ScheduleStore

SLOTS = ["A", "B", "C", "D"]
T0 = datetime(2026, 10, 23, 8, 0, 0)  # A Friday


class FakeDevice:
    mode = "fake"

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self.fail = False

    def trigger_reminder(self, compartment_id, event_id):
        if self.fail:
            raise ConnectionError("port closed")
        self.calls.append(("remind", compartment_id))
        return {"ok": True}

    def stop_alert(self):
        if self.fail:
            raise ConnectionError("port closed")
        self.calls.append(("stop", None))
        return {"ok": True}

    def get_state(self):
        return {"mode": self.mode}


class DoseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.device = FakeDevice()
        self.notifier = Notifier(log_file=self.root / "notifications.jsonl")
        self.schedule = ScheduleStore(self.root / "schedule.json", set(SLOTS))
        self.config = DoseConfig(window_minutes=30, remind_every_minutes=5,
                                 camera_timeout_seconds=30, put_back_grace_minutes=10)
        self.now = T0 - timedelta(minutes=1)
        self.dm = self.make_manager()

    def tearDown(self):
        self.tmp.cleanup()

    def make_manager(self):
        return DoseManager(self.device, self.notifier, self.schedule, SLOTS,
                           sessions_file=self.root / "sessions.json", config=self.config,
                           clock=lambda: self.now)

    def advance(self, seconds=0, minutes=0, tick=True):
        self.now += timedelta(seconds=seconds, minutes=minutes)
        if tick:
            self.dm.tick(self.now)

    def heartbeat(self, states=None, monitoring=True):
        self.dm.heartbeat({"monitoring": monitoring, "states": states or {}}, self.now)

    def camera_sees(self, slot, state):
        self.dm.record_state(slot, state, self.now)

    def kinds(self):
        return [n["kind"] for n in self.notifier.recent(200)]

    def messages(self):
        return " ".join(n["title"] + " " + n["message"] for n in self.notifier.recent(200)).lower()

    def all_loaded(self):
        self.heartbeat({s: "EXPECTED_PRESENT" for s in SLOTS})


class TestScheduledDose(DoseTestBase):
    def setUp(self):
        super().setUp()
        self.schedule.add({"time": "08:00", "compartment_id": "A", "label": "Morning"})
        self.all_loaded()

    def test_dose_starts_at_its_time_and_only_once(self):
        self.advance(seconds=30)  # 07:59:30
        self.assertEqual(self.dm.active_sessions(), [])

        self.advance(seconds=30)  # 08:00:00
        active = self.dm.active_sessions()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].compartment_id, "A")
        self.assertEqual(active[0].label, "Morning")
        self.assertIn(("remind", "A"), self.device.calls)
        self.assertIn("DOSE_DUE", self.kinds())

        for _ in range(10):
            self.advance(seconds=1)
        self.assertEqual(len(self.dm.sessions), 1)

    def test_taken_on_time_closes_removed_and_stops_alert(self):
        self.advance(minutes=1)
        self.heartbeat({s: "EXPECTED_PRESENT" for s in SLOTS})
        self.advance(minutes=2)

        # Candidates and unclear frames never close the dose
        self.camera_sees("A", "REMOVAL_CANDIDATE")
        self.camera_sees("A", "UNCERTAIN")
        self.assertEqual(len(self.dm.active_sessions()), 1)

        self.camera_sees("A", "REMOVAL_DETECTED")
        session = self.dm.sessions[-1]
        self.assertEqual(session.status, CLOSED)
        self.assertEqual(session.outcome, "REMOVED")
        self.assertEqual(self.device.calls[-1], ("stop", None))
        self.assertIn("DOSE_REMOVED", self.kinds())
        # Truthful wording: removal is reported, ingestion is not claimed
        self.assertNotIn("was taken", self.messages())
        self.assertIn("cannot see it being swallowed", self.messages())

        # Duplicate reports and heartbeats do not re-notify or reopen anything
        self.heartbeat({"A": "REMOVAL_DETECTED"})  # steady state: camera reporting
        before = len(self.notifier.recent(200))
        self.camera_sees("A", "REMOVAL_DETECTED")
        for _ in range(40):
            self.advance(minutes=1)
            self.heartbeat({"A": "REMOVAL_DETECTED"})
        self.assertEqual(len(self.notifier.recent(200)), before)
        self.assertEqual(session.outcome, "REMOVED")

    def test_wrong_compartment_warns_and_keeps_dose_open(self):
        self.advance(minutes=1)
        self.camera_sees("C", "REMOVAL_DETECTED")

        session = self.dm.active_sessions()[0]
        self.assertEqual(session.status, ACTIVE)
        self.assertIn("WRONG_COMPARTMENT:C", session.flags)
        self.assertIn("WRONG_COMPARTMENT", self.kinds())
        self.assertNotIn("UNSCHEDULED_REMOVAL", self.kinds())
        self.assertEqual(self.device.calls[-1], ("remind", "A"))  # light re-pointed at A

        self.camera_sees("A", "REMOVAL_DETECTED")
        self.assertEqual(session.outcome, "REMOVED")
        self.assertIn("WRONG_COMPARTMENT:C", session.flags)

    def test_missed_dose_escalates_then_times_out(self):
        self.advance(minutes=1)  # 08:00 dose starts
        for _ in range(30):
            self.advance(minutes=1)
            self.heartbeat({s: "EXPECTED_PRESENT" for s in SLOTS})

        session = self.dm.sessions[-1]
        self.assertEqual(session.outcome, "MISSED")
        self.assertEqual(session.reminders_sent, 6)  # 08:00 + every 5 min until 08:25
        self.assertEqual(self.kinds().count("DOSE_REMINDER"), 5)
        missed = [n for n in self.notifier.recent(200) if n["kind"] == "DOSE_MISSED"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["level"], "critical")
        self.assertEqual(self.device.calls[-1], ("stop", None))

    def test_partial_change_reported_and_outcome_partial(self):
        self.advance(minutes=1)
        self.camera_sees("A", "PARTIAL_OR_WRONG")
        session = self.dm.active_sessions()[0]
        self.assertIn("PARTIAL", session.flags)
        self.assertIn("DOSE_PARTIAL", self.kinds())

        for _ in range(31):
            self.advance(minutes=1)
            self.heartbeat()
        self.assertEqual(session.outcome, "PARTIAL")

    def test_partial_then_emptied_is_removed(self):
        self.advance(minutes=1)
        self.camera_sees("A", "PARTIAL_OR_WRONG")
        self.camera_sees("A", "EMPTY")
        self.assertEqual(self.dm.sessions[-1].outcome, "REMOVED")

    def test_put_back_soon_after_removal_is_flagged(self):
        self.advance(minutes=1)
        self.camera_sees("A", "REMOVAL_DETECTED")
        self.advance(minutes=3)
        self.camera_sees("A", "OBJECT_RETURNED")

        session = self.dm.sessions[-1]
        self.assertIn("PUT_BACK", session.flags)
        self.assertIn("PUT_BACK", self.kinds())

    def test_refill_long_after_removal_is_not_suspicious(self):
        self.advance(minutes=1)
        self.camera_sees("A", "REMOVAL_DETECTED")
        self.advance(minutes=120)
        self.camera_sees("A", "OBJECT_RETURNED")

        self.assertNotIn("PUT_BACK", self.dm.sessions[-1].flags)
        self.assertIn("REFILLED", self.kinds())


class TestCameraAndBoxProblems(DoseTestBase):
    def test_camera_offline_at_dose_time_is_flagged_once(self):
        # No heartbeat at all
        session = self.dm.start_session("B", self.now)
        self.assertIn("CAMERA_OFFLINE", session.flags)
        self.advance(seconds=10)
        self.advance(seconds=10)
        self.assertEqual(self.kinds().count("CAMERA_OFFLINE"), 1)

    def test_camera_dropping_out_during_dose_alerts_and_recovers(self):
        self.all_loaded()
        session = self.dm.start_session("B", self.now)
        self.assertNotIn("CAMERA_OFFLINE", session.flags)

        self.advance(seconds=31)  # heartbeat older than timeout
        self.assertIn("CAMERA_OFFLINE", session.flags)
        self.advance(seconds=5)
        self.assertEqual(self.kinds().count("CAMERA_OFFLINE"), 1)

        self.heartbeat()
        self.assertIn("CAMERA_ONLINE", self.kinds())

    def test_camera_in_trial_mode_is_flagged(self):
        self.dm.heartbeat({"monitoring": False, "trial": True, "states": {}}, self.now)
        session = self.dm.start_session("A", self.now)
        self.assertIn("CAMERA_IN_TRIAL", session.flags)
        self.assertNotIn("CAMERA_OFFLINE", session.flags)
        self.assertIn("CAMERA_IN_TRIAL", self.kinds())

    def test_target_already_empty_is_not_loaded(self):
        self.heartbeat({"A": "REMOVAL_DETECTED", "B": "EXPECTED_PRESENT"})
        session = self.dm.start_session("A", self.now)
        self.assertIn("NOT_LOADED", session.flags)
        self.assertIn("NOT_LOADED", self.kinds())

    def test_device_failure_keeps_dose_running(self):
        self.all_loaded()
        self.device.fail = True
        session = self.dm.start_session("A", self.now)
        self.assertEqual(session.status, ACTIVE)
        self.assertIn("DEVICE_ERROR", session.flags)
        self.assertIn("DEVICE_ERROR", self.kinds())

        self.device.fail = False
        self.camera_sees("A", "REMOVAL_DETECTED")
        self.assertEqual(session.outcome, "REMOVED")

    def test_missed_heartbeat_observation_is_reconciled(self):
        """If the removal POST was lost, the next heartbeat snapshot still closes the dose."""
        self.all_loaded()
        session = self.dm.start_session("A", self.now)
        self.advance(seconds=5)
        self.heartbeat({"A": "REMOVAL_DETECTED"})
        self.assertEqual(session.outcome, "REMOVED")


class TestOutsideDoseTimes(DoseTestBase):
    def test_unscheduled_removal_from_loaded_compartment_alerts(self):
        self.all_loaded()
        self.camera_sees("D", "REMOVAL_DETECTED")
        self.assertIn("UNSCHEDULED_REMOVAL", self.kinds())

    def test_first_observation_after_start_does_not_alert(self):
        # Backend just started: it never saw D loaded, so an empty D is not "a removal".
        self.camera_sees("D", "REMOVAL_DETECTED")
        self.assertEqual(self.kinds(), [])

    def test_manual_reminder_and_cancel(self):
        self.all_loaded()
        self.dm.start_session("C", self.now)
        self.assertEqual(self.dm.cancel_all(self.now), 1)
        self.assertEqual(self.dm.sessions[-1].outcome, "CANCELLED")
        self.assertEqual(self.device.calls[-1], ("stop", None))


class TestScheduleEdgeCases(DoseTestBase):
    def test_overlapping_dose_for_same_compartment_is_skipped(self):
        self.schedule.add({"time": "08:00", "compartment_id": "A"})
        self.schedule.add({"time": "08:10", "compartment_id": "A"})
        self.all_loaded()
        self.advance(minutes=1)
        self.advance(minutes=10)

        outcomes = [(s.status, s.outcome) for s in self.dm.sessions]
        self.assertIn((CLOSED, "SKIPPED"), outcomes)
        self.assertEqual(len(self.dm.active_sessions()), 1)

    def test_two_compartments_due_together(self):
        self.schedule.add({"time": "08:00", "compartment_id": "A"})
        self.schedule.add({"time": "08:00", "compartment_id": "B"})
        self.all_loaded()
        self.advance(minutes=1)
        self.assertEqual(len(self.dm.active_sessions()), 2)

        self.camera_sees("A", "REMOVAL_DETECTED")
        # B still open: the light moves to B instead of switching off
        self.assertEqual(self.device.calls[-1], ("remind", "B"))
        self.assertNotIn("WRONG_COMPARTMENT", self.kinds())

    def test_weekday_filter(self):
        self.schedule.add({"time": "08:00", "compartment_id": "A", "days": [0]})  # Mondays only
        self.advance(minutes=1)  # Friday
        self.assertEqual(self.dm.sessions, [])

    def test_late_start_within_window_but_not_after(self):
        self.schedule.add({"time": "07:45", "compartment_id": "A"})
        self.schedule.add({"time": "07:00", "compartment_id": "B"})
        self.advance(minutes=1)  # 08:00: A is 15 min late (inside 30 min), B is 60 min late
        started = {s.compartment_id for s in self.dm.sessions}
        self.assertEqual(started, {"A"})
        # Deadline counts from the scheduled time, not the late start
        self.assertEqual(self.dm.sessions[0].deadline, "2026-10-23T08:15:00")

    def test_restart_resumes_open_dose_without_refiring(self):
        self.schedule.add({"time": "08:00", "compartment_id": "A"})
        self.all_loaded()
        self.advance(minutes=1)
        self.assertEqual(len(self.dm.sessions), 1)

        # Process restart
        self.dm = self.make_manager()
        self.advance(minutes=1)
        self.assertEqual(len(self.dm.sessions), 1)
        self.assertEqual(len(self.dm.active_sessions()), 1)

        self.advance(minutes=30)
        self.assertEqual(self.dm.sessions[-1].outcome, "MISSED")

    def test_corrupt_sessions_file_starts_clean(self):
        (self.root / "sessions.json").write_text("{not json", encoding="utf-8")
        dm = self.make_manager()
        self.assertEqual(dm.sessions, [])


if __name__ == "__main__":
    unittest.main()
