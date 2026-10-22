"""Caretaker app, patient screen, access control, and the camera recalibration channel."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

import backend_test_env  # noqa: F401  (isolates data/config; must precede backend.app)
import backend.app as backend_app
from backend.app import app
from backend.auth import CaretakerAuth
from backend.commands import CameraCommands

CARE = "http://localhost:5050"   # caretaker port (the only one a tunnel may expose)
H = {"X-MedVerify": "1"}
PASSWORD = "correct horse 42"


class AppsTestBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.tmp = tempfile.TemporaryDirectory()
        backend_app.AUTH = CaretakerAuth(Path(self.tmp.name) / "auth.json")
        backend_app.COMMANDS = CameraCommands()
        doses = backend_app.DOSES
        doses.sessions.clear()
        for c in doses.compartment_states:
            doses.compartment_states[c] = {"state": "UNKNOWN", "updated_at": None}
        doses.camera.update({"last_seen": None, "monitoring": False, "online": False, "trial": False})
        doses._camera_alerted_offline = False
        for slot in list(backend_app.SCHEDULE.slots):
            backend_app.SCHEDULE.delete(slot.id)
        self.local = app.test_client()
        self.son = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def care(self, client, method, path, **kw):
        return client.open(path, method=method, base_url=CARE, **kw)

    def setup_and_login_son(self):
        self.assertEqual(self.local.post("/api/care/setup", json={"password": PASSWORD}).status_code, 200)
        r = self.care(self.son, "POST", "/api/care/login", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200)

    def heartbeat(self, states=None, results=None):
        return self.local.post("/api/vision/heartbeat", json={
            "monitoring": True, "states": states if states is not None else {c: "EXPECTED_PRESENT" for c in "ABCD"},
            "command_results": results or []})

    def kinds(self):
        return [n["kind"] for n in backend_app.NOTIFIER.recent(200)]


class TestAccessControl(AppsTestBase):
    def test_caretaker_port_serves_only_the_caretaker_app(self):
        for path in ("/api/observations", "/api/vision/heartbeat", "/api/patient/silence"):
            self.assertEqual(self.care(self.son, "POST", path, json={}).status_code, 404, path)
        for path in ("/patient", "/console", "/", "/api/status", "/api/patient/state", "/api/schedule"):
            self.assertEqual(self.care(self.son, "GET", path).status_code, 404, path)
        self.assertEqual(self.care(self.son, "GET", "/care").status_code, 200)
        self.assertEqual(self.care(self.son, "GET", "/api/care/session").status_code, 200)

    def test_tunnelled_requests_cannot_reach_local_endpoints(self):
        proxied = {"X-Forwarded-For": "203.0.113.9"}
        self.assertEqual(self.local.post("/api/observations", json={}, headers=proxied).status_code, 403)
        self.assertEqual(self.local.get("/api/patient/state", headers=proxied).status_code, 403)
        self.assertEqual(self.local.get("/api/care/session", headers=proxied).status_code, 200)

    def test_password_can_only_be_created_on_the_laptop_and_once(self):
        r = self.care(self.son, "GET", "/api/care/session").get_json()
        self.assertTrue(r["setup_needed"])
        self.assertFalse(r["can_setup_here"])
        self.assertEqual(self.care(self.son, "POST", "/api/care/setup", json={"password": PASSWORD}).status_code, 403)
        self.assertEqual(self.local.post("/api/care/setup", json={"password": "short"}).status_code, 400)
        self.assertEqual(self.local.post("/api/care/setup", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(self.local.post("/api/care/setup", json={"password": "another one!"}).status_code, 409)
        stored = json.loads(backend_app.AUTH.path.read_text(encoding="utf-8"))
        self.assertNotIn(PASSWORD, json.dumps(stored))  # only a hash is stored

    def test_login_logout_and_lockout(self):
        self.local.post("/api/care/setup", json={"password": PASSWORD})
        self.assertEqual(self.care(self.son, "GET", "/api/care/status").status_code, 401)
        for _ in range(5):
            self.assertEqual(self.care(self.son, "POST", "/api/care/login", json={"password": "nope"}).status_code, 401)
        locked = self.care(self.son, "POST", "/api/care/login", json={"password": PASSWORD})
        self.assertEqual(locked.status_code, 429)
        # A different client is not locked out by someone else's guesses
        other = self.care(self.son, "POST", "/api/care/login", json={"password": PASSWORD},
                          headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(self.care(self.son, "GET", "/api/care/status").status_code, 200)
        self.care(self.son, "POST", "/api/care/logout", headers=H)
        self.assertEqual(self.care(self.son, "GET", "/api/care/status").status_code, 401)

    def test_state_changes_need_the_app_header(self):
        self.setup_and_login_son()
        r = self.care(self.son, "POST", "/api/care/remind", json={"compartment_id": "A"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(backend_app.DOSES.active_sessions(), [])


class TestCaretakerActions(AppsTestBase):
    def test_remind_confirm_cancel_and_schedule(self):
        self.setup_and_login_son()
        self.heartbeat()
        s = self.care(self.son, "POST", "/api/care/remind", json={"compartment_id": "A"}, headers=H).get_json()["session"]
        self.assertEqual((s["compartment_id"], s["reminders_sent"]), ("A", 1))
        again = self.care(self.son, "POST", "/api/care/remind", json={"compartment_id": "A"}, headers=H).get_json()["session"]
        self.assertEqual((again["id"], again["reminders_sent"]), (s["id"], 2))

        r = self.care(self.son, "POST", f"/api/care/doses/{s['id']}/confirm", headers=H)
        self.assertEqual(r.get_json()["session"]["outcome"], "CONFIRMED")
        self.assertEqual(r.get_json()["session"]["closed_by"], "caretaker")
        self.assertEqual(self.care(self.son, "POST", f"/api/care/doses/{s['id']}/cancel", headers=H).status_code, 409)
        self.assertEqual(self.local.get("/api/patient/state").get_json()["mode"], "done")

        b = self.care(self.son, "POST", "/api/care/remind", json={"compartment_id": "B"}, headers=H).get_json()["session"]
        r = self.care(self.son, "POST", f"/api/care/doses/{b['id']}/cancel", headers=H)
        self.assertEqual(r.get_json()["session"]["outcome"], "CANCELLED")

        slot = self.care(self.son, "POST", "/api/care/schedule", json={"time": "08:00", "compartment_id": "C"},
                         headers=H).get_json()["dose"]
        self.care(self.son, "PUT", f"/api/care/schedule/{slot['id']}", json={"enabled": False}, headers=H)
        status = self.care(self.son, "GET", "/api/care/status").get_json()
        self.assertFalse(status["schedule"][0]["enabled"])
        self.assertEqual([h["outcome"] for h in status["history"]], ["CANCELLED", "CONFIRMED"])
        self.assertEqual(self.care(self.son, "DELETE", f"/api/care/schedule/{slot['id']}", headers=H).status_code, 200)


class TestPatientScreen(AppsTestBase):
    def test_idle_due_wrong_and_done(self):
        self.local.post("/api/schedule", json={"time": "23:59", "compartment_id": "D"})
        idle = self.local.get("/api/patient/state").get_json()
        self.assertEqual(idle["mode"], "idle")
        self.assertEqual(idle["next"]["compartment_id"], "D")

        self.heartbeat()
        self.local.post("/api/reminders", json={"compartment_id": "A"})
        due = self.local.get("/api/patient/state").get_json()
        self.assertEqual((due["mode"], due["compartment"], due["silenced"], due["wrong"]), ("due", "A", False, None))

        self.local.post("/api/observations", json={"compartment_id": "C", "state": "REMOVAL_DETECTED"})
        self.assertEqual(self.local.get("/api/patient/state").get_json()["wrong"], "C")

        self.local.post("/api/observations", json={"compartment_id": "A", "state": "REMOVAL_DETECTED"})
        done = self.local.get("/api/patient/state").get_json()
        self.assertEqual((done["mode"], done["compartment"]), ("done", "A"))

    def test_stop_sound_silences_but_keeps_the_dose_open(self):
        self.heartbeat()
        self.local.post("/api/reminders", json={"compartment_id": "B"})
        self.assertEqual(self.local.post("/api/patient/silence").get_json()["silenced"], 1)

        device = backend_app.DEVICE.get_state()
        self.assertFalse(device["buzzer_on"])
        self.assertEqual(device["active_compartment"], "B")  # light stays on
        state = self.local.get("/api/patient/state").get_json()
        self.assertEqual((state["mode"], state["silenced"]), ("due", True))

        # The 5-minute re-reminder still alerts the caretaker but does not beep again
        later = datetime.now() + timedelta(minutes=6)
        self.heartbeat()
        backend_app.DOSES.tick(later)
        self.assertFalse(backend_app.DEVICE.get_state()["buzzer_on"])
        self.assertIn("DOSE_REMINDER", self.kinds())
        self.assertEqual(len(backend_app.DOSES.active_sessions()), 1)

        # Opening the wrong compartment re-alerts (beep + Stop sound button back), consistently
        self.local.post("/api/observations", json={"compartment_id": "D", "state": "REMOVAL_DETECTED"})
        state = self.local.get("/api/patient/state").get_json()
        self.assertEqual((state["silenced"], state["wrong"]), (False, "D"))
        self.assertTrue(backend_app.DEVICE.get_state()["buzzer_on"])


class TestRecalibrationChannel(AppsTestBase):
    def test_refill_recalibration_round_trip(self):
        self.setup_and_login_son()
        self.heartbeat()
        # Dose taken from A a moment ago...
        self.local.post("/api/reminders", json={"compartment_id": "A"})
        self.local.post("/api/observations", json={"compartment_id": "A", "state": "REMOVAL_DETECTED"})

        # ...son refills and presses Recalibrate (twice: still one command)
        first = self.care(self.son, "POST", "/api/care/recalibrate", headers=H).get_json()["command"]
        second = self.care(self.son, "POST", "/api/care/recalibrate", headers=H).get_json()["command"]
        self.assertEqual(first["id"], second["id"])

        hb = self.heartbeat({"A": "REMOVAL_DETECTED"}).get_json()
        self.assertEqual(hb["commands"], [{"id": first["id"], "type": "recalibrate"}])

        # Camera reports success with the new states in the same heartbeat
        hb = self.heartbeat(results=[{"id": first["id"], "ok": True, "message": "saved"}]).get_json()
        self.assertEqual(hb["commands"], [])
        status = self.care(self.son, "GET", "/api/care/status").get_json()
        self.assertEqual(status["commands"][0]["status"], "done")
        self.assertEqual({v["state"] for v in status["compartments"].values()}, {"EXPECTED_PRESENT"})
        self.assertIn("RECALIBRATED", self.kinds())
        self.assertNotIn("PUT_BACK", self.kinds())  # a refill is not "medicine put back"

    def test_failed_and_expired_commands(self):
        self.setup_and_login_son()
        cmd = self.care(self.son, "POST", "/api/care/recalibrate", headers=H).get_json()["command"]
        self.heartbeat(results=[{"id": cmd["id"], "ok": False, "message": "trial mode"}])
        self.assertEqual(backend_app.COMMANDS.recent()[0]["status"], "failed")

        cmds = CameraCommands(expire_seconds=60)
        cmds.enqueue("recalibrate", "caretaker", now=datetime(2026, 1, 1, 8, 0))
        self.assertEqual(cmds.pending(now=datetime(2026, 1, 1, 8, 2)), [])
        self.assertEqual(cmds.recent(now=datetime(2026, 1, 1, 8, 2))[0]["status"], "expired")


class TestCameraSideOfRecalibration(unittest.TestCase):
    def test_event_logger_collects_commands_from_heartbeat_replies(self):
        from vision.event_logger import EventLogger
        reply = MagicMock(status_code=200)
        reply.json.return_value = {"ok": True, "commands": [{"id": "cmd-1", "type": "recalibrate"}, {"bad": 1}]}
        with tempfile.TemporaryDirectory() as tmp, patch("vision.event_logger.requests.post", return_value=reply):
            logger = EventLogger(Path(tmp) / "log.csv", "http://backend.test")
            logger.send_heartbeat({"monitoring": True, "states": {}})
            self.assertTrue(logger.flush(2.0))
            self.assertEqual(logger.take_commands(), [{"id": "cmd-1", "type": "recalibrate"}])
            self.assertEqual(logger.take_commands(), [])
            logger.close()

    def test_inspector_recalibrates_when_still_and_reports_back(self):
        import vision.camera_inspector as ci
        from vision.reference_store import ReferenceStore

        rois = {"A": [0, 0, 100, 100], "B": [100, 0, 100, 100], "C": [0, 100, 100, 100], "D": [100, 100, 100, 100]}
        frame = np.random.default_rng(7).integers(50, 110, (200, 200, 3), dtype=np.uint8)
        for (x, y, w, h) in rois.values():
            cv2.circle(frame, (x + 50, y + 50), 14, (240, 240, 240), -1)

        saved = {k: getattr(ci, k) for k in ("CONFIG_DIR", "DATA_DIR", "SETTINGS_FILE", "ROI_FILE",
                                             "BASELINE_FILE", "STATE_FILE", "LOG_FILE")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ci.CONFIG_DIR, ci.DATA_DIR = root / "config", root / "data"
            ci.SETTINGS_FILE, ci.ROI_FILE = ci.CONFIG_DIR / "settings.json", ci.CONFIG_DIR / "rois.json"
            ci.BASELINE_FILE, ci.STATE_FILE = ci.DATA_DIR / "baseline.npz", ci.DATA_DIR / "state.json"
            ci.LOG_FILE = ci.DATA_DIR / "obs.csv"
            ci.CONFIG_DIR.mkdir(parents=True)
            ci.DATA_DIR.mkdir(parents=True)
            ci.SETTINGS_FILE.write_text(json.dumps({"compartments": list("ABCD"), "camera_index": 0,
                                                    "server_url": "http://backend.test", "monitor_on_start": True}),
                                        encoding="utf-8")
            ci.ROIManager(ci.ROI_FILE).save(rois, frame.shape, camera_index=0)
            store = ReferenceStore(ci.BASELINE_FILE)
            store.save_expected({s: frame[y:y + h, x:x + w].copy() for s, (x, y, w, h) in rois.items()})
            self.assertNotIn("calibration_sharpness", store.metadata)

            sent = []

            def fake_post(url, json=None, timeout=None, **kw):
                sent.append((url, json))
                reply = MagicMock(status_code=200)
                cmds = [{"id": "cmd-9", "type": "recalibrate"}] if url.endswith("/heartbeat") else []
                reply.json.return_value = {"ok": True, "commands": cmds}
                return reply

            clock = iter(i * 0.1 for i in range(100_000))
            with patch("vision.event_logger.requests.post", side_effect=fake_post), \
                 patch("vision.camera_inspector.time") as mock_time, \
                 patch("vision.camera_inspector.open_camera") as mock_open, \
                 patch("vision.camera_inspector.cv2") as mock_cv2:
                mock_time.monotonic.side_effect = lambda: next(clock)
                cap = MagicMock()
                cap.read.return_value = (True, frame)
                mock_open.return_value = cap
                mock_cv2.waitKey.side_effect = [255] * 60 + [ord("q")]
                ci.main()
            try:
                results = [r for _, body in sent if body and body.get("command_results")
                           for r in body["command_results"]]
                self.assertTrue(results, "no command result was reported")
                self.assertEqual(results[0], {"id": "cmd-9", "ok": True, "message": "New 'loaded' photos saved."})
                self.assertIn("calibration_sharpness", ReferenceStore(ci.BASELINE_FILE).metadata)
                # Executed once even though every heartbeat reply repeated the command
                self.assertEqual(len({r["id"] for r in results}), 1)
            finally:
                for k, v in saved.items():
                    setattr(ci, k, v)


if __name__ == "__main__":
    unittest.main()
