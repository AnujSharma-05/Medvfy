"""mv: control MedVerify from a terminal (VS Code) - server, camera, pill box, firmware.

    .\\mv up                     start the server and the camera service in the background
    .\\mv down [camera|server] [--force]   stop them (--force: also MedVerify windows started elsewhere)
    .\\mv status                 what is running, box, camera, compartments, open doses
    .\\mv logs [server|camera]   last lines of a log

    .\\mv box check|test|off     check the ESP32, light A-D in turn, all lights off
    .\\mv box diag               12 s wiring check with a guide to what you should see
    .\\mv box light A            light one compartment (no dose)

    .\\mv cam snapshot           save the current camera image (raw + annotated)
    .\\mv cam regions preview    numbered pen-drawn squares the camera can find (or, with none drawn,
                                medicine groups), with their contents' colour; changes nothing
    .\\mv cam regions auto [A=1 B=3 C=2 D=4]   use them; letters follow the preview numbers if given,
                                otherwise reading order (top row left to right, then the next row)
    .\\mv cam regions set A=x,y,w,h B=... C=... D=...
    .\\mv cam empty              sheet cleared: save the "empty" photos
    .\\mv cam loaded [A=7 B=7 C=4 D=4]   sheet loaded: save the "loaded" photos and start monitoring;
                                with pill counts, each dose is verified by counting
    .\\mv cam monitor on|off

    .\\mv dose start A [pills=2] [label] | cancel      .\\mv schedule list | add 08:00 A [label] | rm ID
    .\\mv trial start A=removed C=changed   (others unchanged)  |  finish  |  eval
    .\\mv firmware build|upload             compile/flash MedVerify_Controller.ino (no Arduino IDE)
    .\\mv demo [S3 S5 ...]                  guided live test of the whole system (you act, it checks)
    .\\mv test                              run the automated tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"
RUN_DIR = ROOT / "data" / "run"
LOG_DIR = ROOT / "data" / "logs"
SETTINGS = ROOT / "config" / "settings.json"
SKETCH = ROOT / "MedVerify_Controller"
FQBN = "esp32:esp32:esp32"
ARDUINO_CLI_CANDIDATES = [
    os.getenv("MEDVERIFY_ARDUINO_CLI", ""),
    str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Arduino IDE" / "resources" / "app" / "lib" / "backend"
        / "resources" / "arduino-cli.exe"),
    r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    "arduino-cli",
]


def settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


BASE = f"http://127.0.0.1:{int(os.getenv('MEDVERIFY_LOCAL_PORT') or settings().get('local_port', 5000))}"


class Fail(SystemExit):
    def __init__(self, message: str) -> None:
        print(f"ERROR: {message}")
        super().__init__(1)


# ------------------------------------------------------------------ http helpers
def api(method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    try:
        r = requests.request(method, BASE + path, json=body, timeout=timeout)
    except requests.RequestException:
        raise Fail("The MedVerify server is not running. Start it with: .\\mv up")
    try:
        data = r.json()
    except ValueError:
        raise Fail(f"{method} {path} -> HTTP {r.status_code}")
    if not r.ok or data.get("ok") is False:
        raise Fail(data.get("error") or f"{method} {path} -> HTTP {r.status_code}")
    return data


def server_up() -> bool:
    try:
        return requests.get(BASE + "/api/health", timeout=1.5).ok
    except requests.RequestException:
        return False


def camera_running(status: dict) -> bool:
    """Reported within the last 6 s. The server keeps a camera 'online' for 30 s after its last
    report (to ride out hiccups), which is too long to tell whether it is running right now."""
    from datetime import datetime
    cam = status["doses"]["camera"]
    if not cam.get("last_seen"):
        return False
    age = (datetime.fromisoformat(status["doses"]["now"]) - datetime.fromisoformat(cam["last_seen"])).total_seconds()
    return age <= 6.0


def camera_command(kind: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    """Send a command to the camera service and wait for its result."""
    status = api("GET", "/api/status")
    if not camera_running(status):
        raise Fail("The camera service is not running. Start it with: .\\mv up")
    cmd = api("POST", "/api/camera/command", {"type": kind, "params": params or {}})["command"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cmd = api("GET", f"/api/camera/command/{cmd['id']}")["command"]
        if cmd["status"] != "pending":
            break
        time.sleep(0.3)
    if cmd["status"] == "pending":
        raise Fail(f"No answer from the camera after {timeout:.0f} s (command still queued).")
    print(cmd["result"])
    if cmd["status"] != "done":
        raise SystemExit(1)
    return cmd


# ----------------------------------------------------------------- processes
def _spawn(name: str, args: list[str]) -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{name}.log", "a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {name} =====\n")
    log.flush()
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # survives the terminal
    proc = subprocess.Popen([str(PY), "-u", *args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    (RUN_DIR / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _managed_pid(name: str) -> int | None:
    """pid of a process mv started, if it is still running."""
    try:
        pid = int((RUN_DIR / f"{name}.pid").read_text())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _foreign_processes() -> list[tuple[int, str]]:
    """MedVerify server/camera processes in this repo not started by mv (e.g. from a .bat)."""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    ours = set()
    for pid_file in RUN_DIR.glob("*.pid"):
        try:
            ours.add(int(pid_file.read_text()))
        except (OSError, ValueError):
            pass
    found = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not any(k in parts[2] for k in ("backend.app", "camera_service", "camera_inspector")):
            continue
        pid, parent = int(parts[0]), int(parts[1] or 0)
        if pid in ours or parent in ours:  # started by mv (the venv launcher spawns a child python)
            continue
        found.append((pid, parts[2].strip()))
    return found


def cmd_up(args: list[str]) -> None:
    if server_up():
        print(f"Server already running at {BASE}.")
    else:
        pid = _spawn("server", ["-m", "backend.app"])
        print(f"Starting server (pid {pid})...", end="", flush=True)
        for _ in range(40):
            if server_up():
                break
            time.sleep(0.5)
            print(".", end="", flush=True)
        print()
        if not server_up():
            raise Fail(f"Server did not start. See: .\\mv logs server\n{_tail(LOG_DIR / 'server.log', 8)}")
    if "--no-camera" not in args:
        # Decide by the process, not by reports: while the server restarts, a running camera's
        # reports pause, and starting a second camera service would fight it for the webcam.
        if _managed_pid("camera"):
            print(f"Camera service already running (pid {_managed_pid('camera')}).")
        elif any("camera_service" in c or "camera_inspector" in c for _, c in _foreign_processes()):
            # Judged by processes only: a camera stopped seconds ago still has a fresh report.
            print("A camera program started outside mv is running; leaving it alone.")
        else:
            print("Starting camera service: the webcam will be on and watching the sheet continuously.")
            pid = _spawn("camera", ["-m", "vision.camera_service"])
            time.sleep(2.0)  # let any report from a camera that just stopped go stale first
            for _ in range(40):
                time.sleep(0.5)
                if not _alive(pid):
                    raise Fail(f"Camera service exited. Last lines of its log:\n{_tail(LOG_DIR / 'camera.log', 4)}")
                if camera_running(api("GET", "/api/status")):
                    break
            else:
                raise Fail(f"Camera service did not report in. See: .\\mv logs camera\n"
                           f"{_tail(LOG_DIR / 'camera.log', 8)}")
            print(f"Camera service running (pid {pid}).")
    cmd_status([])


def cmd_down(args: list[str]) -> None:
    stopped = 0
    only = [a for a in args if a in ("camera", "server")]
    for name in only or ("camera", "server"):
        pid_file = RUN_DIR / f"{name}.pid"
        if pid_file.exists():
            _kill_tree(int(pid_file.read_text()))
            pid_file.unlink()
            print(f"Stopped {name}.")
            stopped += 1
    if "--force" in args:
        for pid, cmdline in _foreign_processes():
            _kill_tree(pid)
            print(f"Stopped pid {pid}: {cmdline[:80]}")
            stopped += 1
    elif _foreign_processes():
        print("Other MedVerify windows are still running (started from a .bat file). Use: .\\mv down --force")
    if not stopped:
        print("Nothing to stop.")


def _tail(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log yet)"


def cmd_logs(args: list[str]) -> None:
    names = [a for a in args if not a.startswith("-")] or ["server", "camera"]
    n = 40
    for a in args:
        if a.startswith("-n"):
            n = int(a[2:] or 40)
    for name in names:
        print(f"----- {name}.log -----")
        print(_tail(LOG_DIR / f"{name}.log", n))


# --------------------------------------------------------------------- status
def cmd_status(args: list[str]) -> None:
    if not server_up():
        print(f"Server: not running ({BASE}). Start everything with: .\\mv up")
        return
    s = api("GET", "/api/status")
    d, dev = s["doses"], s["device"]
    cam = d["camera"]
    det = cam.get("details") or {}
    print(f"Server   : running at {BASE}  (caretaker app on port {settings().get('care_port', 5050)})")
    box = f"{dev.get('mode')}"
    if dev.get("mode") == "serial":
        box += f" on {dev.get('port')}" + (f"  ERROR: {dev['last_error']}" if dev.get("last_error") else "  ok")
    else:
        box += "  (simulated: no real LEDs/buzzer)"
    print(f"Pill box : {box}")
    if not cam.get("online"):
        print("Camera   : not reporting (start it with .\\mv up)")
    else:
        from datetime import datetime
        age = (datetime.fromisoformat(d["now"]) - datetime.fromisoformat(cam["last_seen"])).total_seconds()
        mode = "TRIAL" if cam.get("trial") else ("monitoring" if cam.get("monitoring") else "on, not monitoring")
        print(f"Camera   : {mode}  {det.get('frame_size')}  status {det.get('monitor_status')}  "
              f"sharpness {det.get('sharpness')}/{det.get('sharpness_limit')}  still {det.get('still_seconds')}s"
              + (f"  (last report {age:.0f}s ago: stopped?)" if age > 3 else ""))
        print(f"           regions {'set' if det.get('regions') else 'NOT SET'}, "
              f"loaded photo {'yes' if det.get('has_loaded_photo') else 'NO'}, "
              f"empty photo {'yes' if det.get('has_empty_photo') else 'NO'}"
              + (f", waiting for still scene to {det['waiting_for']}" if det.get("waiting_for") else ""))
        if det.get("view_blocked"):
            print(f"           view blocked (something on the box edge): {', '.join(det['view_blocked'])}")
        trial = det.get("trial")
        if trial:
            stage = "GO (act now)" if trial["ready"] else "checking start, hands off"
            print(f"Trial    : {'running ' + stage if trial['active'] else 'idle'}  labels {trial['labels']}"
                  + (f"  START MISMATCH {trial['start_mismatch']}" if trial["start_mismatch"] else ""))
    live = det.get("live_states") or {}
    def box_text(c: str, v: dict) -> str:
        if v.get("count") is not None:
            return f"{c}={v['count']}/{v.get('loaded_count') or '?'} pills"
        return f"{c}={v['state']}" + (f"({live[c]})" if live.get(c) and live[c] != v["state"] else "")
    comp = "  ".join(box_text(c, v) for c, v in d["compartments"].items())
    est = det.get("count_estimates") or {}
    if est:
        comp += "   (camera estimates: " + ", ".join(f"{k} {v}" for k, v in est.items()) + ")"
    print(f"Boxes    : {comp}")
    for a in d["active_sessions"]:
        print(f"Dose open: {a['compartment_id']} {a['label']} since {a['started_at'][11:16]} "
              f"until {a['deadline'][11:16]} flags {a['flags']}")
    if not d["active_sessions"]:
        nxt = d["upcoming"][0] if d["upcoming"] else None
        print(f"Doses    : none open" + (f"; next {nxt['at'][5:].replace('T', ' ')} {nxt['compartment_id']}" if nxt else ""))
    notes = s.get("notifications", [])[-4:]
    if notes:
        print("Recent alerts:")
        for n in notes:
            print(f"  [{n['level']}] {n['title']}")


# ------------------------------------------------------------------------ box
def _direct_device():
    from backend.device_adapter import ESP32SerialDevice
    port = os.getenv("MEDVERIFY_SERIAL_PORT") or settings().get("serial_port") or "auto"
    return ESP32SerialDevice(port)


DIAG_GUIDE = """
The board reported each step above, so the firmware drove the pins. What did YOU see?

  Blue LED ON THE ESP32 lit, breadboard LEDs did not
      -> board and firmware are fine; the fault is in the breadboard wiring. Most likely, because
         ALL LEDs and the buzzer fail together: the ground. Check that a jumper goes from an ESP32
         pin printed GND to the exact rail the LED short legs and the buzzer '-' use, and that the
         rail is not split in the middle (many breadboards have a gap halfway; bridge it).
         Then: each LED's long leg towards the resistor/GPIO, jumpers on the pins PRINTED 16 17 18 19 25.
  Breadboard LEDs lit, but no tone in step 2
      -> buzzer: S to GPIO25, '-' to GND. If the module has a small transistor on it, its middle
         pin needs 3V3 (check the printed labels first).
  Nothing at all, not even a blue LED on the ESP32
      -> some boards have no LED on GPIO2. Use the breadboard checks above; if still nothing,
         unplug USB, reseat the ESP32 in the breadboard, and run .\\mv box diag again.

Unplug USB before moving any wire. Run .\\mv box diag again after each fix."""


def cmd_box(args: list[str]) -> None:
    if not args:
        raise Fail("Use: box check | test | diag | light A | off")
    action = args[0]
    via_server = server_up()
    if via_server and api("GET", "/api/status")["device"].get("mode") != "serial":
        print("Note: the server runs with a simulated box (device_mode mock). Real LEDs need device_mode serial "
              "in config/settings.json and a restart (.\\mv down; .\\mv up).")
    dev = None if via_server else _direct_device()

    def light(c: str) -> None:
        if via_server:
            api("POST", "/api/device/light", {"compartment_id": c})
        else:
            dev.trigger_reminder(c, "mv")

    def off() -> None:
        if via_server:
            api("POST", "/api/device/off", {})
        else:
            dev.stop_alert()

    try:
        if action == "check":
            result = api("GET", "/api/device/check") if via_server else {**dev.check(), "mode": "serial (direct)"}
            print(f"Box {'answered' if result['ok'] else 'NOT answering'} ({result.get('mode')}, port {result.get('port')})"
                  + (f": {result.get('error')}" if not result["ok"] else ""))
            if not result["ok"]:
                raise SystemExit(1)
        elif action == "test":
            for c in settings().get("compartments", ["A", "B", "C", "D"]):
                print(f"LED {c} on + beep")
                light(c)
                time.sleep(1.5)
            off()
            print("All off. Each compartment's LED should have lit once, in order, with 3 beeps each.")
        elif action == "diag":
            print("Watch the board now: 4 s all LEDs + the ESP32's own blue LED, a 2 s tone, then A-D one by one.")
            report = (api("POST", "/api/device/diag", {}, timeout=40)["report"] if via_server
                      else dev.diagnose())
            for line in report:
                print(f"  board> {line}")
            print(DIAG_GUIDE)
        elif action == "light" and len(args) > 1:
            light(args[1].upper())
            print(f"LED {args[1].upper()} on (beeped). Turn off with: .\\mv box off")
        elif action == "off":
            off()
            print("All LEDs off (open doses stay open).")
        else:
            raise Fail("Use: box check | test | diag | light A | off")
    finally:
        if dev is not None and dev._conn is not None:
            dev._conn.close()


# --------------------------------------------------------------------- camera
def cmd_cam(args: list[str]) -> None:
    if not args:
        raise Fail("Use: cam snapshot | regions auto | regions set ... | empty | loaded | monitor on|off")
    a = args[0]
    if a == "snapshot":
        print("Taking one picture with the webcam...")
        cmd = camera_command("snapshot")
        print(f"Raw image: {cmd['data']['raw']}")
    elif a == "regions" and args[1:2] == ["preview"]:
        print("Taking one picture with the webcam (nothing is changed)...")
        camera_command("set_regions", {"auto": True, "preview": True})
    elif a == "regions" and args[1:2] == ["auto"]:
        # Optional letter assignment from the preview's numbers: A=1 B=3 C=2 D=4
        assign = {}
        for part in args[2:]:
            slot, _, number = part.partition("=")
            if not number.isdigit():
                raise Fail(f"Use letter=number from the preview, e.g. A=1 (got {part})")
            assign[slot.upper()] = int(number)
        cmd = camera_command("set_regions", {"auto": True, **({"assign": assign} if assign else {})})
        for slot, box in cmd["data"]["rois"].items():
            print(f"  {slot}: x={box[0]} y={box[1]} w={box[2]} h={box[3]}")
        print("Check them with: .\\mv cam snapshot  (boxes are drawn on latest_annotated.jpg)")
    elif a == "regions" and args[1:2] == ["set"]:
        rois = {}
        for part in args[2:]:
            slot, _, nums = part.partition("=")
            rois[slot.upper()] = [int(v) for v in nums.split(",")]
        camera_command("set_regions", {"rois": rois})
    elif a == "empty":
        print("Keep hands out of view: the photo is taken once the scene has been still for a second.")
        camera_command("save_empty", timeout=70)
    elif a == "loaded":
        # Optional pill counts per compartment: A=7 B=7 C=4 D=4 (enables counting doses)
        counts = {}
        for part in args[1:]:
            slot, _, n = part.partition("=")
            if not n.isdigit():
                raise Fail(f"Use letter=count, e.g. A=7 (got {part})")
            counts[slot.upper()] = int(n)
        print("Keep hands out of view: the photo is taken once the scene has been still for a second.")
        camera_command("recalibrate", {"counts": counts} if counts else {}, timeout=70)
    elif a == "monitor" and len(args) > 1:
        camera_command("monitor", {"on": args[1].lower() in ("on", "1", "true", "yes")})
    else:
        raise Fail("Use: cam snapshot | regions auto | regions set ... | empty | loaded | monitor on|off")


# ---------------------------------------------------------------- doses/schedule
def cmd_dose(args: list[str]) -> None:
    if args[:1] == ["start"] and len(args) > 1:
        rest = args[2:]
        pills = 1
        if rest and rest[0].startswith("pills="):
            pills = int(rest.pop(0).split("=", 1)[1])
        s = api("POST", "/api/reminders", {"compartment_id": args[1].upper(), "label": " ".join(rest),
                                           "pills": pills})["session"]
        print(f"Dose opened for {s['compartment_id']} ({s['dose_size']} pill(s)) until {s['deadline'][11:16]}. "
              f"Count at start: {s['start_count']}. Flags: {s['flags'] or 'none'}")
    elif args[:1] == ["cancel"]:
        r = api("POST", "/api/device/stop", {})
        print(f"Cancelled {r['event']['cancelled_sessions']} open dose(s); lights off.")
    else:
        raise Fail("Use: dose start A [label] | dose cancel")


def cmd_schedule(args: list[str]) -> None:
    if not args or args[0] == "list":
        doses = api("GET", "/api/schedule")["doses"]
        for d in doses:
            print(f"  {d['id']}  {d['time']}  {d['compartment_id']}  {'on ' if d['enabled'] else 'off'}  {d['label']}")
        if not doses:
            print("  (no dose times)")
    elif args[0] == "add" and len(args) >= 3:
        d = api("POST", "/api/schedule", {"time": args[1], "compartment_id": args[2].upper(),
                                          "label": " ".join(args[3:])})["dose"]
        print(f"Added {d['id']}: {d['time']} compartment {d['compartment_id']}")
    elif args[0] == "rm" and len(args) == 2:
        api("DELETE", f"/api/schedule/{args[1]}")
        print("Removed.")
    else:
        raise Fail("Use: schedule list | add 08:00 A [label] | rm ID")


# ---------------------------------------------------------------------- trials
def cmd_trial(args: list[str]) -> None:
    if args[:1] == ["start"]:
        labels = {}
        for part in args[1:]:
            slot, _, label = part.partition("=")
            labels[slot.upper()] = label.lower()
        camera_command("trial_start", {"labels": labels})
        print("Hands off...", end="", flush=True)
        for _ in range(60):
            det = api("GET", "/api/status")["doses"]["camera"].get("details") or {}
            trial = det.get("trial") or {}
            if trial.get("start_mismatch"):
                print(f"\nThe sheet does not match the 'loaded' photo in {trial['start_mismatch']}. "
                      "Put it back as calibrated and start again.")
                raise SystemExit(1)
            if trial.get("ready"):
                print("\nGO: do it now, move your hand out of view, wait ~4 s, then: .\\mv trial finish")
                return
            time.sleep(0.5)
            print(".", end="", flush=True)
        raise Fail("The scene never settled (something moving in view?).")
    elif args[:1] == ["finish"]:
        camera_command("trial_finish")
    elif args[:1] == ["eval"]:
        subprocess.run([str(PY), str(ROOT / "tools" / "evaluate_trials.py"), *args[1:]], cwd=ROOT)
    else:
        raise Fail("Use: trial start A=removed C=changed | trial finish | trial eval [--apply]")


# -------------------------------------------------------------------- firmware
def _arduino_cli() -> str:
    for c in ARDUINO_CLI_CANDIDATES:
        if not c:
            continue
        if c == "arduino-cli" or Path(c).exists():
            try:
                subprocess.run([c, "version"], capture_output=True, check=True)
                return c
            except (OSError, subprocess.CalledProcessError):
                continue
    raise Fail("arduino-cli not found. Install the Arduino IDE or set MEDVERIFY_ARDUINO_CLI.")


def cmd_firmware(args: list[str]) -> None:
    cli = _arduino_cli()
    action = args[0] if args else "build"
    build_dir = ROOT / "data" / "firmware-build"
    print(f"Compiling {SKETCH.name} for {FQBN}...")
    r = subprocess.run([cli, "compile", "--fqbn", FQBN, "--output-dir", str(build_dir), str(SKETCH)])
    if r.returncode != 0:
        raise Fail("Compilation failed (see the messages above).")
    if action == "build":
        print(f"Built. Output in {build_dir}")
        return
    if action != "upload":
        raise Fail("Use: firmware build | firmware upload")
    if server_up() and api("GET", "/api/status")["device"].get("mode") == "serial":
        raise Fail("The server is using the board's USB port. Run .\\mv down first, then upload.")
    from backend.device_adapter import find_esp32_ports
    ports = find_esp32_ports()
    port = os.getenv("MEDVERIFY_SERIAL_PORT") or (ports[0] if len(ports) == 1 else "")
    if not port:
        raise Fail(f"Could not pick the board's port (found: {ports or 'none'}). Set MEDVERIFY_SERIAL_PORT.")
    print(f"Uploading to {port}...")
    r = subprocess.run([cli, "upload", "--fqbn", FQBN, "-p", port, "--input-dir", str(build_dir), str(SKETCH)])
    if r.returncode != 0:
        raise Fail("Upload failed. If it says 'Failed to connect', hold the board's BOOT button while it says "
                   "'Connecting...'.")
    time.sleep(2.0)
    result = _direct_device().check()
    print("Board answers after upload." if result["ok"] else f"Board did not answer: {result.get('error')}")


def cmd_demo(args: list[str]) -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("medverify_demo", ROOT / "tools" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    sys.modules["medverify_demo"] = demo  # dataclasses need the module registered
    spec.loader.exec_module(demo)
    if not server_up():
        raise Fail("The server is not running. Start everything with: .\\mv up")
    results = demo.Demo(demo.RequestsHTTP(BASE)).run([a.upper() for a in args] or None)
    demo.report(results)


def cmd_test(args: list[str]) -> None:
    raise SystemExit(subprocess.run([str(PY), "-m", "unittest", "discover", "-s", "tests", *args], cwd=ROOT).returncode)


COMMANDS = {"up": cmd_up, "down": cmd_down, "status": cmd_status, "logs": cmd_logs, "box": cmd_box,
            "cam": cmd_cam, "dose": cmd_dose, "schedule": cmd_schedule, "trial": cmd_trial,
            "firmware": cmd_firmware, "test": cmd_test, "demo": cmd_demo}


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in COMMANDS:
        print(__doc__)
        raise SystemExit(0 if not argv or argv[0] in ("-h", "--help", "help") else 1)
    COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
