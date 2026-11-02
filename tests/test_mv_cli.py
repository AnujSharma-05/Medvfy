"""tools/mv.py: argument handling and safety checks (no processes started, no network)."""

import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mv", ROOT / "tools" / "mv.py")
mv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mv)


class TestMvCli(unittest.TestCase):
    def run_mv(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                mv.main(list(argv))
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_help_and_unknown(self):
        code, out = self.run_mv()
        self.assertEqual(code, 0)
        self.assertIn("mv up", out)
        self.assertEqual(self.run_mv("fly")[0], 1)

    def test_server_down_gives_a_clear_message(self):
        with patch.object(mv, "server_up", return_value=False):
            code, out = self.run_mv("status")
        self.assertIn("not running", out)
        with patch.object(mv.requests, "request", side_effect=mv.requests.ConnectionError()):
            code, out = self.run_mv("dose", "start", "A")
        self.assertEqual(code, 1)
        self.assertIn(".\\mv up", out)

    def test_camera_counts_as_running_only_with_a_fresh_report(self):
        def status(age):
            return {"doses": {"now": "2026-10-23T15:00:30",
                              "camera": {"online": True, "last_seen": f"2026-10-23T15:00:{30 - age:02d}"}}}
        self.assertTrue(mv.camera_running(status(1)))
        self.assertFalse(mv.camera_running(status(20)))  # still "online" for the server, but stopped
        self.assertFalse(mv.camera_running({"doses": {"now": "2026-10-23T15:00:30", "camera": {"last_seen": None}}}))

    def test_regions_auto_assignment_parsed(self):
        with patch.object(mv, "camera_command") as cc:
            cc.return_value = {"data": {"rois": {"A": [1, 2, 3, 4]}}}
            self.run_mv("cam", "regions", "auto", "a=1", "B=3")
        cc.assert_called_once_with("set_regions", {"auto": True, "assign": {"A": 1, "B": 3}})
        self.assertEqual(self.run_mv("cam", "regions", "auto", "A=blue")[0], 1)

    def test_regions_set_parses_boxes(self):
        with patch.object(mv, "camera_command") as cc:
            self.run_mv("cam", "regions", "set", "a=1,2,30,40", "B=5,6,70,80")
        cc.assert_called_once_with("set_regions", {"rois": {"A": [1, 2, 30, 40], "B": [5, 6, 70, 80]}})

    def test_trial_labels_parsed(self):
        with patch.object(mv, "camera_command") as cc, \
             patch.object(mv, "api", return_value={"doses": {"camera": {"details": {"trial": {"ready": True,
                                                                                         "start_mismatch": []}}}}}):
            code, out = self.run_mv("trial", "start", "A=Removed", "c=changed")
        cc.assert_called_once_with("trial_start", {"labels": {"A": "removed", "C": "changed"}})
        self.assertIn("GO", out)

    def test_firmware_upload_refuses_while_server_holds_the_port(self):
        with patch.object(mv, "_arduino_cli", return_value="arduino-cli"), \
             patch.object(mv.subprocess, "run") as run, \
             patch.object(mv, "server_up", return_value=True), \
             patch.object(mv, "api", return_value={"device": {"mode": "serial"}}):
            run.return_value.returncode = 0
            code, out = self.run_mv("firmware", "upload")
        self.assertEqual(code, 1)
        self.assertIn("mv down", out)
        # Only the compile ran; no upload was attempted
        self.assertTrue(all("upload" not in call.args[0] for call in run.call_args_list))

    def test_own_processes_are_not_reported_as_foreign(self):
        listing = ("100|1|python.exe -m backend.app\n"      # mv's launcher
                   "101|100|python.exe -m backend.app\n"    # its child
                   "200|7|python.exe -m backend.app\n")     # started from a .bat
        with patch.object(mv.os, "name", "nt"), \
             patch.object(mv.subprocess, "run") as run, \
             patch.object(mv, "RUN_DIR") as run_dir:
            run.return_value.stdout = listing
            pid_file = unittest.mock.MagicMock()
            pid_file.read_text.return_value = "100"
            run_dir.glob.return_value = [pid_file]
            found = mv._foreign_processes()
        self.assertEqual([pid for pid, _ in found], [200])


if __name__ == "__main__":
    unittest.main()
