"""Basic offline API tests using the mock device; no hardware required."""
import unittest
from backend.app import app


class MedVerifyApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_valid_reminder(self):
        response = self.client.post("/api/reminders", json={"compartment_id": "B"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["event"]["compartment_id"], "B")

    def test_invalid_compartment(self):
        response = self.client.post("/api/reminders", json={"compartment_id": "Z"})
        self.assertEqual(response.status_code, 400)

    def test_observation(self):
        response = self.client.post("/api/observations", json={"compartment_id": "A", "state": "CHANGED", "changed_pixel_fraction": 0.12})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
