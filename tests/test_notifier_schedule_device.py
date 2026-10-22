"""Notifier channels, schedule store, and device adapters (all offline)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from backend.device_adapter import ESP32HTTPDevice, ESP32SerialDevice, MockDevice, build_device
from backend.notifier import CRITICAL, NtfyChannel, Notifier, build_notifier
from backend.schedule import ScheduleError, ScheduleStore

SLOTS = {"A", "B", "C", "D"}


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "n.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_in_app_feed_and_log(self):
        n = Notifier(log_file=self.log)
        n.notify("Hello", "World", kind="TEST")
        self.assertEqual(n.recent()[0]["title"], "Hello")
        self.assertEqual(n.recent()[0]["deliveries"], {"in_app": "delivered"})
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])["kind"], "TEST")

    @patch("backend.notifier.requests.post")
    def test_ntfy_payload(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        n = Notifier(channels=[NtfyChannel("medverify-abc123", "https://ntfy.example", token="tok")])
        note = n.notify("Missed dose: compartment A", "Not emptied within 30 minutes.", level=CRITICAL)
        self.assertTrue(n.flush(2.0))

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://ntfy.example/medverify-abc123")
        self.assertEqual(kwargs["data"], "Not emptied within 30 minutes.".encode())
        self.assertEqual(kwargs["headers"]["Title"], "Missed dose: compartment A")
        self.assertEqual(kwargs["headers"]["Priority"], "urgent")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(note.deliveries["ntfy"], "delivered")

    @patch("backend.notifier.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_push_failure_is_recorded_not_hidden(self, _):
        n = Notifier(channels=[NtfyChannel("t")])
        note = n.notify("x", "y")
        self.assertTrue(n.flush(2.0))
        self.assertTrue(note.deliveries["ntfy"].startswith("failed: ConnectionError"))
        self.assertEqual(note.deliveries["in_app"], "delivered")

    @patch("backend.notifier.requests.post")
    def test_slow_push_does_not_block_notify(self, mock_post):
        import threading
        import time

        gate = threading.Event()
        mock_post.side_effect = lambda *a, **k: (gate.wait(2.0), MagicMock(status_code=200))[1]
        n = Notifier(channels=[NtfyChannel("t")])
        start = time.monotonic()
        n.notify("x", "y")
        self.assertLess(time.monotonic() - start, 0.5)
        gate.set()
        self.assertTrue(n.flush(2.0))

    def test_build_notifier_push_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEDVERIFY_NTFY_TOPIC", None)
            self.assertEqual(build_notifier({}).channel_names, ["in_app"])
            off = {"notifications": {"ntfy": {"enabled": False, "topic": "abc"}}}
            self.assertEqual(build_notifier(off).channel_names, ["in_app"])
            on = {"notifications": {"ntfy": {"enabled": True, "topic": "abc"}}}
            self.assertEqual(build_notifier(on).channel_names, ["in_app", "ntfy"])

    def test_env_topic_enables_push(self):
        with patch.dict(os.environ, {"MEDVERIFY_NTFY_TOPIC": "secret-topic"}):
            n = build_notifier({})
        self.assertEqual(n.channel_names, ["in_app", "ntfy"])
        self.assertEqual(n.channels[0].topic, "secret-topic")


class TestScheduleStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "schedule.json"
        self.store = ScheduleStore(self.path, SLOTS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation(self):
        for bad in ({"time": "8:00", "compartment_id": "A"}, {"time": "24:00", "compartment_id": "A"},
                    {"time": "08:00", "compartment_id": "Z"}, {"time": "08:00", "compartment_id": "A", "days": [7]}):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                self.store.add(bad)

    def test_persist_update_delete(self):
        slot = self.store.add({"time": "08:30", "compartment_id": "b", "label": "Morning"})
        self.assertEqual(slot.compartment_id, "B")
        reloaded = ScheduleStore(self.path, SLOTS)
        self.assertEqual(reloaded.list()[0]["time"], "08:30")

        self.store.update(slot.id, {"enabled": False})
        self.assertFalse(ScheduleStore(self.path, SLOTS).list()[0]["enabled"])
        with self.assertRaises(ScheduleError):
            self.store.update(slot.id, {"time": "nope"})
        self.assertTrue(self.store.delete(slot.id))
        self.assertFalse(self.store.delete(slot.id))
        self.assertEqual(ScheduleStore(self.path, SLOTS).list(), [])

    def test_due_and_upcoming(self):
        self.store.add({"time": "08:00", "compartment_id": "A"})
        self.store.add({"time": "20:00", "compartment_id": "B", "enabled": False})
        now = datetime(2026, 10, 23, 8, 10)
        due = self.store.due(now, timedelta(minutes=30))
        self.assertEqual([s.compartment_id for s, _ in due], ["A"])
        self.assertEqual(self.store.due(now + timedelta(minutes=30), timedelta(minutes=30)), [])
        upcoming = self.store.next_occurrences(now)
        self.assertEqual(upcoming[0]["at"], "2026-10-24T08:00")
        self.assertEqual(len(upcoming), 1)  # disabled dose not listed

    def test_corrupt_file_is_reported(self):
        self.path.write_text("{oops", encoding="utf-8")
        store = ScheduleStore(self.path, SLOTS)
        self.assertEqual(store.list(), [])
        self.assertIn("Could not load", store.load_error)


class FakeSerial:
    def __init__(self, port, baud):
        self.port, self.baud = port, baud
        self.written: list[bytes] = []
        self.is_open = True
        self.fail_next = False

    def write(self, data):
        if self.fail_next:
            self.fail_next = False
            raise OSError("device unplugged")
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        return b"REMINDER: ok\r\n"

    def close(self):
        self.is_open = False


class TestDeviceAdapters(unittest.TestCase):
    def test_serial_speaks_existing_firmware_protocol(self):
        conns = []
        dev = ESP32SerialDevice("COM9", serial_factory=lambda p, b: conns.append(FakeSerial(p, b)) or conns[-1])
        dev.trigger_reminder("b", "evt-1")
        dev.stop_alert()
        self.assertEqual(conns[0].written, [b"B\n", b"STOP\n"])
        self.assertEqual(dev.get_state()["active_compartment"], None)
        self.assertEqual(dev.last_response, "REMINDER: ok")

    def test_serial_reconnects_after_error(self):
        conns = []
        dev = ESP32SerialDevice("COM9", serial_factory=lambda p, b: conns.append(FakeSerial(p, b)) or conns[-1])
        dev.trigger_reminder("A", "e")
        conns[0].fail_next = True
        with self.assertRaises(OSError):
            dev.trigger_reminder("A", "e")
        self.assertIn("unplugged", dev.get_state()["last_error"])
        dev.trigger_reminder("C", "e")
        self.assertEqual(len(conns), 2)
        self.assertEqual(conns[1].written, [b"C\n"])
        self.assertIsNone(dev.last_error)

    def test_build_device_modes(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("MEDVERIFY_DEVICE", "MEDVERIFY_SERIAL_PORT", "MEDVERIFY_ESP32_URL"):
                os.environ.pop(key, None)
            self.assertIsInstance(build_device({}), MockDevice)
            self.assertIsInstance(build_device({"device_mode": "serial", "serial_port": "COM3"}), ESP32SerialDevice)
            self.assertIsInstance(build_device({"device_mode": "esp32"}), ESP32HTTPDevice)
            with self.assertRaises(ValueError):
                build_device({"device_mode": "serial"})
            os.environ["MEDVERIFY_DEVICE"] = "mock"
            self.assertIsInstance(build_device({"device_mode": "serial"}), MockDevice)
            os.environ.pop("MEDVERIFY_DEVICE")


if __name__ == "__main__":
    unittest.main()
