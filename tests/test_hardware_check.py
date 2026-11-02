"""tools/hardware_check.py with scripted answers and a firmware-like fake board."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.device_adapter import ESP32SerialDevice

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hardware_check", ROOT / "tools" / "hardware_check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class Board:
    """Replies like MedVerify_Controller.ino; records what it was told."""

    def __init__(self, silent=False):
        self.silent, self.lines, self.sent, self.is_open = silent, [], [], True

    def reset_input_buffer(self):
        self.lines.clear()

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if self.silent:
            return
        reply = {"STOP": "Reminder cleared. All LEDs and buzzer OFF.", "HELP": "=== MedVerify ESP32 controller ==="}
        self.lines.append((reply.get(cmd) or f"REMINDER: compartment {cmd} selected; LED ON").encode() + b"\r\n")

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        self.is_open = False


def device_on(board):
    def make(port, *a, **k):
        dev = ESP32SerialDevice(port, serial_factory=lambda p, b: board)
        dev.REPLY_TIMEOUT_S, dev.BOOT_WAIT_S = 0.05, 0.01
        return dev
    return make


def scripted(*answers):
    it = iter(answers)
    return lambda question, options: options[next(it)]


class TestHardwareCheck(unittest.TestCase):
    def test_all_good(self):
        board = Board()
        with patch.object(hc, "ESP32SerialDevice", device_on(board)):
            result = hc.run("COM28", answer=scripted("y", "y", "y", "y", "y"), pause=0)
        self.assertTrue(result["ok"])
        self.assertEqual(board.sent, ["HELP", "A", "B", "C", "D", "STOP"])

    def test_faults_map_to_wiring_tips(self):
        with patch.object(hc, "ESP32SerialDevice", device_on(Board())):
            result = hc.run("COM28", answer=scripted("l", "w", "b", "b", "n"), pause=0)
        self.assertFalse(result["ok"])
        text = "\n".join(result["problems"])
        self.assertIn("LED A (GPIO16) did not light", text)
        self.assertIn("P17", text)
        self.assertIn("GPIO25", text)
        self.assertEqual(text.count("No beep"), 1)  # same fault reported once
        self.assertIn("stayed on after STOP", text)

    def test_silent_board_stops_before_lighting_anything(self):
        board = Board(silent=True)
        with patch.object(hc, "ESP32SerialDevice", device_on(board)):
            result = hc.run("COM28", answer=scripted(), pause=0)
        self.assertFalse(result["ok"])
        self.assertIn("did not answer", result["problems"][0])
        self.assertEqual(board.sent, ["HELP", "HELP"])  # one resend on a fresh connection, no LED commands

    def test_save_switches_to_serial_auto_and_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"device_mode": "mock", "camera_index": 0}), encoding="utf-8")
            hc.save_serial_settings(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"device_mode": "serial", "camera_index": 0, "serial_port": "auto"})


if __name__ == "__main__":
    unittest.main()
