"""Backend HTTP API: schedule, heartbeat, observations driving dose sessions, notifications, console."""

import unittest
from datetime import datetime, timedelta

import backend_test_env  # noqa: F401  (isolates data/config; must precede backend.app)
import backend.app as backend_app
from backend.app import app


class BackendApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        doses = backend_app.DOSES
        doses.sessions.clear()
        for c in doses.compartment_states:
            doses.compartment_states[c] = {"state": "UNKNOWN", "updated_at": None}
        doses.camera.update({"last_seen": None, "monitoring": False, "online": False})
        doses._camera_alerted_offline = False
        for slot in list(backend_app.SCHEDULE.slots):
            backend_app.SCHEDULE.delete(slot.id)

    def heartbeat(self, states, monitoring=True):
        return self.client.post("/api/vision/heartbeat", json={"monitoring": monitoring, "states": states})

    def test_data_is_isolated_from_repo(self):
        self.assertIn("medverify-tests-", str(backend_app.DATA_DIR))
        self.assertIn("medverify-tests-", str(backend_app.SCHEDULE.path))

    def test_console_page_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"MedVerify", r.data)
        self.assertIn(b"/api/status", r.data)
        r.close()

    def test_schedule_crud(self):
        r = self.client.post("/api/schedule", json={"time": "08:00", "compartment_id": "a", "label": "Morning"})
        self.assertEqual(r.status_code, 201)
        slot_id = r.get_json()["dose"]["id"]

        self.assertEqual(self.client.post("/api/schedule", json={"time": "8am", "compartment_id": "A"}).status_code, 400)
        r = self.client.put(f"/api/schedule/{slot_id}", json={"enabled": False})
        self.assertFalse(r.get_json()["dose"]["enabled"])
        self.assertEqual(self.client.put("/api/schedule/nope", json={}).status_code, 404)

        doses = self.client.get("/api/schedule").get_json()["doses"]
        self.assertEqual([d["compartment_id"] for d in doses], ["A"])
        self.assertEqual(self.client.delete(f"/api/schedule/{slot_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/schedule/{slot_id}").status_code, 404)

    def test_full_dose_flow_through_http(self):
        """Reminder -> camera heartbeat -> camera observation -> dose closed REMOVED."""
        self.heartbeat({c: "EXPECTED_PRESENT" for c in "ABCD"})
        r = self.client.post("/api/reminders", json={"compartment_id": "B"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["event"]["compartment_id"], "B")
        self.assertEqual(body["session"]["status"], "ACTIVE")
        self.assertEqual(body["device"]["active_compartment"], "B")

        status = self.client.get("/api/status").get_json()
        self.assertTrue(status["doses"]["camera"]["online"])
        self.assertEqual(len(status["doses"]["active_sessions"]), 1)

        # Candidate observation: shown, not acted on
        self.client.post("/api/observations", json={"compartment_id": "B", "state": "REMOVAL_CANDIDATE",
                                                     "previous_state": "EXPECTED_PRESENT"})
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["doses"]["compartments"]["B"]["pending"], "REMOVAL_CANDIDATE")
        self.assertEqual(len(status["doses"]["active_sessions"]), 1)

        r = self.client.post("/api/observations", json={"compartment_id": "B", "state": "REMOVAL_DETECTED",
                                                        "previous_state": "REMOVAL_CANDIDATE"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["observation"]["previous_state"], "REMOVAL_CANDIDATE")

        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["doses"]["active_sessions"], [])
        self.assertEqual(status["doses"]["recent_sessions"][0]["outcome"], "REMOVED")
        self.assertEqual(status["counts"]["REMOVED"], 1)
        self.assertIsNone(status["device"]["active_compartment"])
        kinds = [n["kind"] for n in status["notifications"]]
        self.assertIn("DOSE_DUE", kinds)
        self.assertIn("DOSE_REMOVED", kinds)

    def test_stop_cancels_open_dose(self):
        self.client.post("/api/reminders", json={"compartment_id": "A"})
        r = self.client.post("/api/device/stop", json={})
        self.assertEqual(r.get_json()["event"]["cancelled_sessions"], 1)
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["doses"]["recent_sessions"][0]["outcome"], "CANCELLED")

    def test_scheduled_dose_fires_on_tick(self):
        now = datetime.now().replace(second=0, microsecond=0)
        self.client.post("/api/schedule", json={"time": now.strftime("%H:%M"), "compartment_id": "C"})
        backend_app.DOSES.tick(now + timedelta(seconds=5))
        active = self.client.get("/api/status").get_json()["doses"]["active_sessions"]
        self.assertEqual([d["compartment_id"] for d in active], ["C"])

    def test_heartbeat_validation(self):
        r = self.client.post("/api/vision/heartbeat", json={"states": ["A"]})
        self.assertEqual(r.status_code, 400)

    def test_test_notification(self):
        r = self.client.post("/api/notifications/test", json={})
        self.assertEqual(r.get_json()["channels"], ["in_app"])
        feed = self.client.get("/api/notifications").get_json()
        self.assertEqual(feed["notifications"][-1]["kind"], "TEST")


if __name__ == "__main__":
    unittest.main()
