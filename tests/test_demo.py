"""tools/demo.py run against the real server, with a simulated camera that follows the instructions."""

import importlib.util
import re
import unittest
from pathlib import Path

import backend_test_env  # noqa: F401  (isolates data/config; must precede backend.app)
import backend.app as backend_app
from backend.app import app
from backend.commands import CameraCommands

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("medverify_demo", ROOT / "tools" / "demo.py")
demo = importlib.util.module_from_spec(spec)
__import__("sys").modules["medverify_demo"] = demo  # dataclasses need the module registered
spec.loader.exec_module(demo)

LOADED = {"A": 4, "B": 4, "C": 4, "D": 4}


class FlaskHTTP:
    def __init__(self, client):
        self.client = client

    def get(self, path):
        return self.client.get(path).get_json()

    def post(self, path, body, timeout=15):
        data = self.client.post(path, json=body).get_json()
        if data.get("ok") is False:
            raise RuntimeError(data.get("error"))
        return data


class SimulatedCamera:
    """Reacts to the demo's instructions the way the real camera should (or, if blind, not at all)."""

    def __init__(self, client, blind=False):
        self.client, self.blind = client, blind
        self.counts = dict(LOADED)
        self.report()

    def report(self):
        self.client.post("/api/vision/heartbeat", json={
            "monitoring": True, "states": {c: "EXPECTED_PRESENT" for c in LOADED},
            "counts": dict(self.counts), "loaded_counts": dict(LOADED)})

    def follow(self, instruction):
        if self.blind:
            return self.report()
        take = re.search(r"[Tt]ake (ONE|TWO) pieces? out of (?:compartment )?([A-D])", instruction)
        if take:
            slot = take.group(2)
            self.counts[slot] -= 1 if take.group(1) == "ONE" else 2
            self.report()
            if "put it back" in instruction:
                self.counts[slot] += 1
        back = re.search(r"back into compartment ([A-D])", instruction)
        if back:
            self.counts[back.group(1)] = LOADED[back.group(1)]
        self.report()


class Operator:
    """The person at the box: answers yes, and does what the last instruction said."""

    def __init__(self, camera):
        self.camera, self.last, self.questions = camera, "", []

    def out(self, text):
        if ">>>" in text:
            self.last = text

    def ask(self, prompt):
        self.questions.append(prompt.strip())
        if "Press Enter" in prompt:
            self.camera.follow(self.last)
            return ""
        return "y"


class TestDemo(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        backend_app.COMMANDS = CameraCommands()
        doses = backend_app.DOSES
        doses.sessions.clear()
        for c in doses.compartment_states:
            doses.compartment_states[c] = {"state": "UNKNOWN", "updated_at": None}
        self.client = app.test_client()
        self.t = [0.0]

    def make(self, blind=False):
        camera = SimulatedCamera(self.client, blind)
        op = Operator(camera)
        tick = lambda s=0.0: self.t.__setitem__(0, self.t[0] + max(s, 0.5))  # noqa: E731
        d = demo.Demo(FlaskHTTP(self.client), ask=op.ask, out=op.out, clock=lambda: self.t[0], sleep=tick)
        return d, op

    def test_every_scenario_passes_with_a_working_camera(self):
        d, op = self.make()
        results = {r.id: r for r in d.run()}
        self.assertIsNone(results["S1"].passed)  # simulated box in tests: hardware step skipped
        for rid in ("S2", "S3", "S4", "S5", "S6", "S7", "S8"):
            self.assertTrue(results[rid].passed, f"{rid}: {results[rid].detail}")
        self.assertIn("TOO_MANY", results["S6"].detail)
        self.assertIn("PUT_BACK", results["S4"].detail)
        self.assertTrue(any("Is LED A on" in q for q in op.questions))
        self.assertEqual(backend_app.DOSES.active_sessions(), [])  # demo leaves no dose open

    def test_a_blind_camera_fails_the_dose_scenarios(self):
        d, _ = self.make(blind=True)
        results = {r.id: r for r in d.run(["S3", "S5", "S6", "S7"])}
        self.assertFalse(results["S3"].passed)
        self.assertIn("still open", results["S3"].detail)
        self.assertFalse(results["S5"].passed)
        self.assertFalse(results["S6"].passed)
        self.assertTrue(results["S7"].passed)  # nothing changes: correct for a blind camera too

    def test_preflight_refuses_without_counts(self):
        out = []
        d = demo.Demo(FlaskHTTP(self.client), ask=lambda p: "y", out=out.append, sleep=lambda s: None)
        self.client.post("/api/vision/heartbeat", json={"monitoring": True, "states": {}})
        self.assertEqual(d.run(), [])
        self.assertTrue(any("No pill counts" in line for line in out))

    def test_report_saves_results(self):
        r = [demo.Result("S2", "idle", True, "no change")]
        path = demo.report(r, out=lambda s: None)
        try:
            self.assertTrue(path.exists())
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
