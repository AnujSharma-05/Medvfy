"""MedVerify local backend: schedule, dose sessions, device control, notifications, web console.

Run:  .venv\\Scripts\\python.exe -m backend.app   then open http://127.0.0.1:5000

Paths can be redirected with MEDVERIFY_DATA_DIR / MEDVERIFY_CONFIG_DIR (used by tests so
they never write into the real data folder).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from backend.device_adapter import MockDevice, build_device
from backend.dose_manager import DoseConfig, DoseManager
from backend.notifier import WARNING, build_notifier
from backend.schedule import ScheduleError, ScheduleStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.getenv("MEDVERIFY_CONFIG_DIR") or ROOT / "config")
DATA_DIR = Path(os.getenv("MEDVERIFY_DATA_DIR") or ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = ROOT / "config" / "settings.json"  # Settings are always read from the repo config
EVENTS_FILE = DATA_DIR / "events.jsonl"
OBSERVATIONS_FILE = DATA_DIR / "observations.jsonl"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


SETTINGS = load_settings()
COMPARTMENTS = [str(c).upper() for c in SETTINGS.get("compartments", ["A", "B", "C", "D"])]
COMPARTMENT_SET = set(COMPARTMENTS)

NOTIFIER = build_notifier(SETTINGS, DATA_DIR / "notifications.jsonl")
try:
    DEVICE = build_device(SETTINGS)
except Exception as exc:  # Misconfigured hardware must not stop reminders and notifications
    DEVICE = MockDevice()
    NOTIFIER.notify("Pill box hardware not configured",
                    f"{exc}. Running in mock mode: lights/buzzer will not work.", WARNING, "DEVICE_ERROR")
SCHEDULE = ScheduleStore(CONFIG_DIR / "schedule.json", COMPARTMENT_SET)
DOSES = DoseManager(
    DEVICE, NOTIFIER, SCHEDULE, COMPARTMENTS,
    sessions_file=DATA_DIR / "dose_sessions.json",
    config=DoseConfig.from_settings(SETTINGS),
)
RECENT_EVENTS: list[dict] = []
RECENT_OBSERVATIONS: list[dict] = []

app = Flask(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def remember_event(event: dict) -> None:
    RECENT_EVENTS.append(event)
    del RECENT_EVENTS[:-100]
    append_jsonl(EVENTS_FILE, event)


# --------------------------------------------------------------------------- pages
@app.get("/")
def home():
    return send_from_directory(STATIC_DIR, "console.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "medverify-local", "device_mode": DEVICE.mode, "timestamp": utc_now()})


# ----------------------------------------------------------------------- reminders
@app.post("/api/reminders")
def create_reminder():
    """Start a dose session now for one compartment (manual reminder / test)."""
    body = request.get_json(silent=True) or {}
    compartment = str(body.get("compartment_id", "")).upper()
    if compartment not in COMPARTMENT_SET:
        return jsonify({"ok": False, "error": f"compartment_id must be one of {', '.join(COMPARTMENTS)}"}), 400
    session = DOSES.start_session(compartment, label=str(body.get("label", ""))[:60])
    event = {
        "event_id": str(body.get("event_id") or f"evt-{uuid.uuid4().hex[:10]}"),
        "timestamp": utc_now(),
        "type": "REMINDER",
        "compartment_id": compartment,
        "session_id": session.id,
        "status": "DEVICE_ERROR" if "DEVICE_ERROR" in session.flags else "SENT_TO_DEVICE_ADAPTER",
    }
    remember_event(event)
    return jsonify({"ok": True, "event": event, "session": session.to_dict(), "device": DEVICE.get_state()})


@app.post("/api/device/stop")
def stop_device_alert():
    """Stop the alert. Open doses are closed as CANCELLED so reminders do not restart."""
    cancelled = DOSES.cancel_all()
    if not cancelled:
        try:
            DEVICE.stop_alert()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
    event = {"event_id": f"evt-{uuid.uuid4().hex[:10]}", "timestamp": utc_now(), "type": "STOP_ALERT",
             "status": "SENT_TO_DEVICE_ADAPTER", "cancelled_sessions": cancelled}
    remember_event(event)
    return jsonify({"ok": True, "event": event, "device": DEVICE.get_state()})


# -------------------------------------------------------------------- vision input
@app.post("/api/observations")
def record_observation():
    body = request.get_json(silent=True) or {}
    compartment = str(body.get("compartment_id", "")).upper()
    if compartment and compartment not in COMPARTMENT_SET:
        return jsonify({"ok": False, "error": f"compartment_id must be one of {', '.join(COMPARTMENTS)}"}), 400
    allowed = {"compartment_id", "state", "previous_state", "changed_pixel_fraction", "before_image",
               "after_image", "note"}
    obs = {k: v for k, v in body.items() if k in allowed}
    obs["timestamp"] = utc_now()
    obs["type"] = "VISION_OBSERVATION"
    RECENT_OBSERVATIONS.append(obs)
    del RECENT_OBSERVATIONS[:-100]
    append_jsonl(OBSERVATIONS_FILE, obs)
    if compartment and obs.get("state"):
        DOSES.record_state(compartment, str(obs["state"]).upper())
    return jsonify({"ok": True, "observation": obs}), 201


@app.post("/api/vision/heartbeat")
def vision_heartbeat():
    body = request.get_json(silent=True) or {}
    states = body.get("states") or {}
    if not isinstance(states, dict):
        return jsonify({"ok": False, "error": "states must be an object"}), 400
    DOSES.heartbeat({
        "monitoring": bool(body.get("monitoring", False)),
        "trial": bool(body.get("trial", False)),
        "camera_index": body.get("camera_index"),
        "states": {str(k).upper(): str(v).upper() for k, v in states.items()},
    })
    return jsonify({"ok": True})


# ------------------------------------------------------------------------ schedule
@app.get("/api/schedule")
def list_schedule():
    return jsonify({"ok": True, "doses": SCHEDULE.list(), "load_error": SCHEDULE.load_error})


@app.post("/api/schedule")
def add_schedule():
    try:
        slot = SCHEDULE.add(request.get_json(silent=True) or {})
    except ScheduleError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "dose": slot.to_dict()}), 201


@app.put("/api/schedule/<slot_id>")
def update_schedule(slot_id: str):
    try:
        slot = SCHEDULE.update(slot_id, request.get_json(silent=True) or {})
    except KeyError:
        return jsonify({"ok": False, "error": "dose not found"}), 404
    except ScheduleError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "dose": slot.to_dict()})


@app.delete("/api/schedule/<slot_id>")
def delete_schedule(slot_id: str):
    if not SCHEDULE.delete(slot_id):
        return jsonify({"ok": False, "error": "dose not found"}), 404
    return jsonify({"ok": True})


# ------------------------------------------------------------------- notifications
@app.get("/api/notifications")
def list_notifications():
    limit = max(1, min(200, request.args.get("limit", 50, type=int)))
    return jsonify({"ok": True, "channels": NOTIFIER.channel_names, "notifications": NOTIFIER.recent(limit)})


@app.post("/api/notifications/test")
def test_notification():
    n = NOTIFIER.notify("MedVerify test notification",
                       "If you can read this, notifications are working.", kind="TEST")
    return jsonify({"ok": True, "notification": n.to_dict(), "channels": NOTIFIER.channel_names})


# -------------------------------------------------------------------------- status
@app.get("/api/status")
def status():
    doses = DOSES.status()
    counts = {"REMINDER": 0, "STOP_ALERT": 0, "DEVICE_ERROR": 0, **doses["outcome_counts"]}
    for ev in RECENT_EVENTS:
        if ev.get("type") in ("REMINDER", "STOP_ALERT"):
            counts[ev["type"]] += 1
        if ev.get("status") == "DEVICE_ERROR":
            counts["DEVICE_ERROR"] += 1
    return jsonify({
        "ok": True,
        "timestamp": utc_now(),
        "device": DEVICE.get_state(),
        "counts": counts,
        "events": RECENT_EVENTS[-100:],
        "observations": RECENT_OBSERVATIONS[-100:],
        "doses": doses,
        "notification_channels": NOTIFIER.channel_names,
        "notifications": NOTIFIER.recent(30),
        "schedule_error": SCHEDULE.load_error,
    })


# ------------------------------------------------------------------- background
_scheduler_started = False


def start_scheduler(interval_s: float = 1.0) -> None:
    """Tick the dose manager in the background (starts doses, re-reminds, times out)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop() -> None:
        while True:
            try:
                DOSES.tick()
            except Exception as exc:  # Never let one bad tick stop all future reminders
                print(f"[SCHEDULER] tick failed: {exc}")
            time.sleep(interval_s)

    threading.Thread(target=loop, name="medverify-scheduler", daemon=True).start()


if __name__ == "__main__":
    print("MedVerify local server starting...")
    print("Device mode:", DEVICE.mode)
    print("Notification channels:", ", ".join(NOTIFIER.channel_names))
    print(f"Schedule: {len(SCHEDULE.slots)} dose time(s) from {SCHEDULE.path}")
    start_scheduler()
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
