"""Import this before `backend.app` so tests never touch the real data/ or config/ folders.

backend.app resolves its data and schedule paths at import time from
MEDVERIFY_DATA_DIR / MEDVERIFY_CONFIG_DIR. Without this, a test reminder would be
written to data/dose_sessions.json and show up as a real open dose on the next run.
It also forces mock hardware and no phone push, whatever settings.json says.
"""
import atexit
import os
import shutil
import tempfile

if "MEDVERIFY_TEST_ROOT" not in os.environ:
    _root = tempfile.mkdtemp(prefix="medverify-tests-")
    os.environ["MEDVERIFY_TEST_ROOT"] = _root
    os.environ["MEDVERIFY_DATA_DIR"] = os.path.join(_root, "data")
    os.environ["MEDVERIFY_CONFIG_DIR"] = os.path.join(_root, "config")
    os.environ["MEDVERIFY_DEVICE"] = "mock"
    os.environ.pop("MEDVERIFY_NTFY_TOPIC", None)
    atexit.register(shutil.rmtree, _root, ignore_errors=True)
