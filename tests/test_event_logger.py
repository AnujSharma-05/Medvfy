"""Unit tests for EventLogger: new-transition filtering and non-blocking backend delivery."""

import csv
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from vision.event_logger import EventLogger
from vision.state_machine import CompartmentState, StateTransition


def _transition(slot: str, prev: CompartmentState, new: CompartmentState) -> StateTransition:
    return StateTransition(
        slot=slot,
        previous_state=prev,
        new_state=new,
        timestamp_utc="2026-10-22T00:00:00+00:00",
        is_new_event=(prev != new),
        reason="test",
        metrics={"composite_diff": 0.3},
    )


REMOVED = _transition("A", CompartmentState.EXPECTED_PRESENT, CompartmentState.REMOVAL_DETECTED)
STILL_REMOVED = _transition("A", CompartmentState.REMOVAL_DETECTED, CompartmentState.REMOVAL_DETECTED)
STILL_PRESENT = _transition("B", CompartmentState.EXPECTED_PRESENT, CompartmentState.EXPECTED_PRESENT)


class TestEventLoggerFiltering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "obs.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _rows(self) -> list[list[str]]:
        with self.log_file.open("r", encoding="utf-8") as f:
            return list(csv.reader(f))[1:]

    def test_only_new_transitions_are_written(self):
        """Regression: every evaluation was logged, so a stable scene produced a row per press."""
        logger = EventLogger(self.log_file)
        logger.log_transitions([REMOVED, STILL_PRESENT])
        logger.log_transitions([STILL_REMOVED, STILL_PRESENT])

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:3], ["A", "REMOVAL_DETECTED"])

    def test_no_file_created_when_nothing_new(self):
        EventLogger(self.log_file).log_transitions([STILL_REMOVED, STILL_PRESENT])
        self.assertFalse(self.log_file.exists())

    def test_opt_out_logs_every_observation(self):
        logger = EventLogger(self.log_file)
        logger.log_transitions([REMOVED, STILL_PRESENT], only_new_events=False)
        self.assertEqual(len(self._rows()), 2)

    @patch("vision.event_logger.requests.post")
    def test_only_new_transitions_are_posted(self, mock_post):
        """Regression: duplicates were POSTed to the backend as fresh notifications."""
        mock_post.return_value = MagicMock(status_code=201)
        logger = EventLogger(self.log_file, "http://backend.test")
        logger.log_transitions([REMOVED, STILL_PRESENT])
        logger.log_transitions([STILL_REMOVED, STILL_REMOVED])
        self.assertTrue(logger.flush(timeout=2.0))

        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["compartment_id"], "A")
        self.assertEqual(payload["state"], "REMOVAL_DETECTED")
        self.assertEqual(logger.delivered_count, 1)
        logger.close()


class TestEventLoggerNonBlockingDelivery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "obs.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_slow_backend_does_not_block_caller(self):
        """Regression: log_transitions waited on each HTTP POST (up to 1 s per compartment)."""
        release = threading.Event()

        def slow_post(*args, **kwargs):
            release.wait(timeout=2.0)
            return MagicMock(status_code=201)

        with patch("vision.event_logger.requests.post", side_effect=slow_post):
            logger = EventLogger(self.log_file, "http://backend.test")
            started = time.monotonic()
            logger.log_transitions([REMOVED])
            elapsed = time.monotonic() - started

            # The CSV row is written before log_transitions returns, independent of delivery.
            self.assertLess(elapsed, 0.5)
            self.assertTrue(self.log_file.exists())
            self.assertEqual(logger.delivered_count, 0)

            release.set()
            self.assertTrue(logger.flush(timeout=2.0))
            self.assertEqual(logger.delivered_count, 1)
            logger.close()

    def test_backend_failure_is_counted_not_hidden(self):
        with patch(
            "vision.event_logger.requests.post",
            side_effect=requests.ConnectionError("refused"),
        ):
            logger = EventLogger(self.log_file, "http://backend.test")
            logger.log_transitions([REMOVED])
            self.assertTrue(logger.flush(timeout=2.0))

        self.assertEqual(logger.delivered_count, 0)
        self.assertEqual(logger.failed_count, 1)
        self.assertIn("ConnectionError", logger.last_delivery_error)
        # Local record survives the failed delivery
        self.assertTrue(self.log_file.exists())
        logger.close()

    def test_full_queue_drops_remote_copy_but_keeps_csv(self):
        release = threading.Event()

        def blocked_post(*args, **kwargs):
            release.wait(timeout=2.0)
            return MagicMock(status_code=201)

        with patch("vision.event_logger.requests.post", side_effect=blocked_post):
            logger = EventLogger(self.log_file, "http://backend.test", max_pending=1)
            events = [
                _transition(s, CompartmentState.EXPECTED_PRESENT, CompartmentState.REMOVAL_DETECTED)
                for s in "ABCD"
            ]
            logger.log_transitions(events)
            release.set()
            self.assertTrue(logger.flush(timeout=2.0))

            self.assertGreaterEqual(logger.dropped_count, 1)
            self.assertEqual(logger.delivered_count + logger.dropped_count, 4)
            with self.log_file.open("r", encoding="utf-8") as f:
                self.assertEqual(len(list(csv.reader(f))) - 1, 4)
            logger.close()

    def test_no_server_url_means_no_delivery_attempt(self):
        with patch("vision.event_logger.requests.post") as mock_post:
            logger = EventLogger(self.log_file)
            logger.log_transitions([REMOVED])
            self.assertTrue(logger.flush(timeout=1.0))
            mock_post.assert_not_called()
            self.assertTrue(logger.close())


if __name__ == "__main__":
    unittest.main()
