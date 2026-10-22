"""MedVerify local backend: schedule, dose sessions, device control, notifications, and the apps.

Run:  .venv\\Scripts\\python.exe -m backend.app

Two listeners, both on 127.0.0.1:
    LOCAL port (5000)  everything on this laptop: patient screen (/patient), developer console
                       (/console), camera endpoints, and the caretaker app for local setup.
    CARE port  (5050)  the caretaker app ONLY (/care, /api/care/*). A public tunnel must point
                       here and never at the local port, so the internet can reach the son's
                       app but not the camera endpoints, patient screen, or console.
As a second line of defence, requests on the local port that carry proxy headers (i.e. came
through a tunnel) are refused for anything except the caretaker app.

Paths can be redirected with MEDVERIFY_DATA_DIR / MEDVERIFY_CONFIG_DIR (used by tests so
they never write into the real data folder).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory, session

from backend.auth import AuthError, CaretakerAuth
from backend.commands import CameraCommands
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
LOCAL_PORT = int(os.getenv("MEDVERIFY_LOCAL_PORT") or SETTINGS.get("local_port", 5000))
CARE_PORT = int(os.getenv("MEDVERIFY_CARE_PORT") or SETTINGS.get("care_port", 5050))

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
AUTH = CaretakerAuth(CONFIG_DIR / "auth.json")
COMMANDS = CameraCommands()
RECENT_EVENTS: list[dict] = []
RECENT_OBSERVATIONS: list[dict] = []

app = Flask(__name__)
app.secret_key = AUTH.secret_key
app.config.update(
    SESSION_COOKIE_NAME="medverify_care",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-Ip", "Cf-Connecting-Ip", "X-Forwarded-Host")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def remember_event(event: dict) -> None:
    RECENT_EVENTS.append(event)
    del RECENT_EVENTS[:-100]
    append_jsonl(EVENTS_FILE, event)


# ------------------------------------------------------------------- access control
def is_care_path(path: str) -> bool:
    return path == "/care" or path.startswith("/care/") or path.startswith("/api/care/")


def via_proxy() -> bool:
    return any(h in request.headers for h in PROXY_HEADERS)


def on_care_port() -> bool:
    return str(request.environ.get("SERVER_PORT")) == str(CARE_PORT)


def is_local_request() -> bool:
    """On this laptop, on the local port, not relayed by a tunnel."""
    return not on_care_port() and not via_proxy()


def client_id() -> str:
    fwd = request.headers.get("Cf-Connecting-Ip") or request.headers.get("X-Forwarded-For", "").split(",")[0]
    return fwd.strip() or request.remote_addr or "unknown"


@app.before_request
def gate() -> None:
    path = request.path
    if is_care_path(path):
        return None
    if on_care_port():
        abort(404)  # The public-facing port serves the caretaker app and nothing else
    if via_proxy():
        abort(403)  # Tunnelled traffic must never reach camera, patient, or console endpoints
    return None


def care_required(view):
    """Logged-in caretaker; state-changing calls must carry the X-MedVerify header (blocks CSRF:
    a cross-site page cannot add a custom header without a CORS preflight, which we never allow)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("care"):
            return jsonify({"ok": False, "error": "Please log in."}), 401
        if request.method != "GET" and request.headers.get("X-MedVerify") != "1":
            return jsonify({"ok": False, "error": "Missing request header."}), 400
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- pages
@app.get("/")
def home():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/console")
def console():
    return send_from_directory(STATIC_DIR, "console.html")


@app.get("/patient")
def patient_page():
    return send_from_directory(STATIC_DIR, "patient.html")


@app.get("/care")
def care_page():
    return send_from_directory(STATIC_DIR, "care.html")


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
    session_ = DOSES.start_session(compartment, label=str(body.get("label", ""))[:60])
    event = {
        "event_id": str(body.get("event_id") or f"evt-{uuid.uuid4().hex[:10]}"),
        "timestamp": utc_now(),
        "type": "REMINDER",
        "compartment_id": compartment,
        "session_id": session_.id,
        "status": "DEVICE_ERROR" if "DEVICE_ERROR" in session_.flags else "SENT_TO_DEVICE_ADAPTER",
    }
    remember_event(event)
    return jsonify({"ok": True, "event": event, "session": session_.to_dict(), "device": DEVICE.get_state()})


@app.post("/api/device/stop")
def stop_device_alert():
    """Developer console: stop the alert and close open doses as CANCELLED."""
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
    """Camera liveness + state snapshot in; pending camera commands out."""
    body = request.get_json(silent=True) or {}
    states = body.get("states") or {}
    if not isinstance(states, dict):
        return jsonify({"ok": False, "error": "states must be an object"}), 400
    # Results first: a finished recalibration resets states before the snapshot is applied,
    # so the refill is not mistaken for medicine being put back.
    for result in body.get("command_results") or []:
        if not isinstance(result, dict):
            continue
        done = COMMANDS.complete(str(result.get("id", "")), bool(result.get("ok")), str(result.get("message", "")))
        if done and done["type"] == "recalibrate" and done["status"] == "done":
            DOSES.mark_recalibrated(by=done["requested_by"])
    DOSES.heartbeat({
        "monitoring": bool(body.get("monitoring", False)),
        "trial": bool(body.get("trial", False)),
        "camera_index": body.get("camera_index"),
        "states": {str(k).upper(): str(v).upper() for k, v in states.items()},
    })
    return jsonify({"ok": True, "commands": COMMANDS.pending()})


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


# ---------------------------------------------------------------- patient screen
def patient_state(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    active = DOSES.active_sessions()
    if active:
        s = active[-1]
        wrong = [f.split(":", 1)[1] for f in s.flags if f.startswith("WRONG_COMPARTMENT:")]
        return {"mode": "due", "compartment": s.compartment_id, "label": s.label,
                "silenced": bool(s.silenced_at), "wrong": wrong[-1] if wrong else None,
                "others": [o.compartment_id for o in active[:-1]], "deadline": s.deadline}
    for s in reversed(DOSES.sessions):
        if s.outcome in ("REMOVED", "CONFIRMED") and s.closed_at:
            if now - datetime.fromisoformat(s.closed_at) <= timedelta(minutes=10):
                return {"mode": "done", "compartment": s.compartment_id, "at": s.closed_at}
            break
    nxt = SCHEDULE.next_occurrences(now, 1)
    return {"mode": "idle", "next": nxt[0] if nxt else None}


@app.get("/api/patient/state")
def patient_state_api():
    return jsonify({"ok": True, **patient_state()})


@app.post("/api/patient/silence")
def patient_silence():
    count = DOSES.silence(by="patient")
    return jsonify({"ok": True, "silenced": count})


# ---------------------------------------------------------------- caretaker app
@app.get("/api/care/session")
def care_session():
    return jsonify({"ok": True, "logged_in": bool(session.get("care")), "setup_needed": AUTH.setup_needed,
                    # The first password can only be created on the laptop itself.
                    "can_setup_here": AUTH.setup_needed and is_local_request()})


@app.post("/api/care/setup")
def care_setup():
    if not AUTH.setup_needed:
        return jsonify({"ok": False, "error": "A password is already set. Log in instead."}), 409
    if not is_local_request():
        return jsonify({"ok": False, "error": "Create the password on the home laptop first."}), 403
    body = request.get_json(silent=True) or {}
    try:
        AUTH.set_password(str(body.get("password", "")))
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    session.clear()
    session["care"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.post("/api/care/login")
def care_login():
    body = request.get_json(silent=True) or {}
    who = client_id()
    wait = AUTH.locked_for(who)
    if wait > 0:
        return jsonify({"ok": False, "error": f"Too many wrong passwords. Try again in {int(wait // 60) + 1} min."}), 429
    if AUTH.setup_needed:
        return jsonify({"ok": False, "error": "No password yet. Create one on the home laptop."}), 409
    if not AUTH.check(str(body.get("password", "")), who):
        return jsonify({"ok": False, "error": "Wrong password."}), 401
    session.clear()
    session["care"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.post("/api/care/logout")
def care_logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/care/password")
@care_required
def care_change_password():
    body = request.get_json(silent=True) or {}
    try:
        AUTH.set_password(str(body.get("new_password", "")), current=str(body.get("current_password", "")))
    except AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/api/care/status")
@care_required
def care_status():
    doses = DOSES.status()
    return jsonify({
        "ok": True,
        "now": doses["now"],
        "camera": doses["camera"],
        "device": {"mode": DEVICE.mode, "error": DEVICE.get_state().get("last_error")},
        "compartments": doses["compartments"],
        "active": doses["active_sessions"],
        "history": [s for s in doses["recent_sessions"] if s["status"] == "CLOSED"][:15],
        "upcoming": doses["upcoming"],
        "schedule": SCHEDULE.list(),
        "notifications": NOTIFIER.recent(25)[::-1],
        "commands": COMMANDS.recent(),
        "compartment_ids": COMPARTMENTS,
    })


@app.post("/api/care/remind")
@care_required
def care_remind():
    compartment = str((request.get_json(silent=True) or {}).get("compartment_id", "")).upper()
    if compartment not in COMPARTMENT_SET:
        return jsonify({"ok": False, "error": "Choose a compartment."}), 400
    s = DOSES.remind_now(compartment, by="caretaker")
    return jsonify({"ok": True, "session": s.to_dict()})


@app.post("/api/care/doses/<session_id>/<action>")
@care_required
def care_dose_action(session_id: str, action: str):
    if action == "cancel":
        s = DOSES.cancel_session(session_id, by="caretaker")
    elif action == "confirm":
        s = DOSES.confirm_session(session_id, by="caretaker")
    else:
        abort(404)
    if s is None:
        return jsonify({"ok": False, "error": "That dose is no longer open."}), 409
    return jsonify({"ok": True, "session": s.to_dict()})


@app.post("/api/care/recalibrate")
@care_required
def care_recalibrate():
    cmd = COMMANDS.enqueue("recalibrate", requested_by="caretaker")
    return jsonify({"ok": True, "command": cmd})


@app.post("/api/care/schedule")
@care_required
def care_add_schedule():
    return add_schedule()


@app.put("/api/care/schedule/<slot_id>")
@care_required
def care_update_schedule(slot_id: str):
    return update_schedule(slot_id)


@app.delete("/api/care/schedule/<slot_id>")
@care_required
def care_delete_schedule(slot_id: str):
    return delete_schedule(slot_id)


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


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve() -> None:
    from werkzeug.serving import make_server

    # On Windows a second server can bind the same port silently (address reuse) and the
    # two then split the requests. Refuse to start instead of running half-invisible.
    busy = [p for p in (LOCAL_PORT, CARE_PORT) if port_in_use(p)]
    if busy:
        print(f"ERROR: port(s) {', '.join(map(str, busy))} already in use. Is MedVerify already running? "
              "Close the other server window first.", flush=True)
        raise SystemExit(1)
    local = make_server("127.0.0.1", LOCAL_PORT, app, threaded=True)
    care = make_server("127.0.0.1", CARE_PORT, app, threaded=True)
    threading.Thread(target=care.serve_forever, name="medverify-care", daemon=True).start()
    try:
        local.serve_forever()
    finally:
        care.shutdown()


if __name__ == "__main__":
    print("MedVerify local server starting...")
    print("Device mode:", DEVICE.mode)
    print("Notification channels:", ", ".join(NOTIFIER.channel_names))
    print(f"Schedule: {len(SCHEDULE.slots)} dose time(s) from {SCHEDULE.path}")
    start_scheduler()
    print(f"Patient screen:   http://127.0.0.1:{LOCAL_PORT}/patient")
    print(f"Caretaker app:    http://127.0.0.1:{CARE_PORT}/care   (share this port through a tunnel)")
    print(f"Developer console http://127.0.0.1:{LOCAL_PORT}/console")
    if AUTH.setup_needed:
        print(f"First run: create the caretaker password at http://127.0.0.1:{LOCAL_PORT}/care")
    serve()
