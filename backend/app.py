from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import uuid
from flask import Flask, jsonify, request, render_template_string
from backend.device_adapter import build_device

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"
OBSERVATIONS_FILE = DATA_DIR / "observations.jsonl"
COMPARTMENTS = {"A", "B", "C", "D"}
DEVICE = build_device()
RECENT_EVENTS: list[dict] = []
RECENT_OBSERVATIONS: list[dict] = []

app = Flask(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MedVerify Local Console</title>
<style>
:root{color-scheme:dark;font-family:system-ui,Segoe UI,sans-serif;background:#101729;color:#e8edf8}body{max-width:920px;margin:32px auto;padding:0 18px}h1{margin-bottom:4px}.muted{color:#a9b5ce}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.slot,section{background:#18233b;border:1px solid #30405f;border-radius:14px;padding:16px}.slot{cursor:pointer;color:#fff;font-size:18px}.slot.active{border:2px solid #71d6a0;background:#1b3b3b}.row{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}button{background:#284469;border:0;border-radius:9px;color:white;padding:11px 15px;cursor:pointer}.danger{background:#713744}.status{white-space:pre-wrap;font-family:ui-monospace,monospace;background:#0b1220;padding:14px;border-radius:10px;min-height:44px}.small{font-size:13px}.event{padding:8px 0;border-bottom:1px solid #30405f}@media(max-width:540px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<h1>MedVerify</h1><div class="muted">Local development console · Mock hardware mode</div>
<p>This console simulates reminders without requiring an ESP32 or physical LEDs. It does not prove medication was swallowed.</p>
<section><h2>Choose compartment</h2><div class="grid" id="slots">
<button class="slot active" data-slot="A">Compartment A</button><button class="slot" data-slot="B">Compartment B</button><button class="slot" data-slot="C">Compartment C</button><button class="slot" data-slot="D">Compartment D</button></div>
<div class="row"><button id="remind">Trigger reminder</button><button id="stop" class="danger">Stop alert</button><button id="refresh">Refresh status</button></div>
<h3>System status</h3><div class="status" id="status">Loading...</div></section>
<section style="margin-top:16px"><h2>Recent events</h2><div id="events" class="muted">No events yet.</div></section>
<section style="margin-top:16px"><h2>Recent observations</h2><div id="observations" class="muted">No observations yet.</div></section>
<p class="muted small">Tonight: software-only simulation. Tomorrow: configure and test the ESP32 adapter after confirming exact board/module markings, wiring, and local network.</p>
<script>
let selected='A';
document.querySelectorAll('[data-slot]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.slot;document.querySelectorAll('[data-slot]').forEach(x=>x.classList.toggle('active',x===b));}));
async function api(path,method='GET',body){let opts={method,headers:{'Content-Type':'application/json'}};if(body)opts.body=JSON.stringify(body);let r=await fetch(path,opts);let j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j;}
async function refresh(){try{let s=await api('/api/status');document.getElementById('status').textContent=JSON.stringify({device:s.device,counts:s.counts},null,2);document.getElementById('events').innerHTML=s.events.length?s.events.slice().reverse().map(e=>`<div class="event">${e.timestamp} · ${e.type} · ${e.compartment_id||'-'} · ${e.status||''}</div>`).join(''):'No events yet.';document.getElementById('observations').innerHTML=s.observations.length?s.observations.slice().reverse().map(o=>`<div class="event">${o.timestamp} · ${o.compartment_id||'-'} · ${o.state||'OBSERVED'} · change ${o.changed_pixel_fraction??'n/a'}</div>`).join(''):'No observations yet.';}catch(e){document.getElementById('status').textContent='Error: '+e.message;}}
document.getElementById('remind').addEventListener('click',async()=>{try{let r=await api('/api/reminders','POST',{compartment_id:selected});document.getElementById('status').textContent=JSON.stringify(r,null,2);if('Notification'in window&&Notification.permission==='granted')new Notification('MedVerify reminder',{body:'Please check compartment '+selected+'.'});await refresh();}catch(e){document.getElementById('status').textContent='Error: '+e.message;}});
document.getElementById('stop').addEventListener('click',async()=>{try{let r=await api('/api/device/stop','POST',{});document.getElementById('status').textContent=JSON.stringify(r,null,2);await refresh();}catch(e){document.getElementById('status').textContent='Error: '+e.message;}});
document.getElementById('refresh').addEventListener('click',refresh);refresh();setInterval(refresh,4000);
</script></body></html>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "medverify-local", "device_mode": DEVICE.mode, "timestamp": utc_now()})


@app.post("/api/reminders")
def create_reminder():
    body = request.get_json(silent=True) or {}
    compartment = str(body.get("compartment_id", "")).upper()
    if compartment not in COMPARTMENTS:
        return jsonify({"ok": False, "error": "compartment_id must be A, B, C, or D"}), 400
    event_id = str(body.get("event_id") or f"evt-{uuid.uuid4().hex[:10]}")
    event = {"event_id": event_id, "timestamp": utc_now(), "type": "REMINDER", "compartment_id": compartment, "status": "REQUESTED"}
    try:
        result = DEVICE.trigger_reminder(compartment, event_id)
        event["status"] = "SENT_TO_DEVICE_ADAPTER"
        event["device_result"] = result
    except Exception as exc:
        event["status"] = "DEVICE_ERROR"
        event["error"] = str(exc)
        RECENT_EVENTS.append(event)
        append_jsonl(EVENTS_FILE, event)
        return jsonify({"ok": False, "event": event, "error": str(exc)}), 502
    RECENT_EVENTS.append(event)
    del RECENT_EVENTS[:-100]
    append_jsonl(EVENTS_FILE, event)
    return jsonify({"ok": True, "event": event, "device": result})


@app.post("/api/device/stop")
def stop_device_alert():
    try:
        result = DEVICE.stop_alert()
        event = {"event_id": f"evt-{uuid.uuid4().hex[:10]}", "timestamp": utc_now(), "type": "STOP_ALERT", "status": "SENT_TO_DEVICE_ADAPTER", "device_result": result}
        RECENT_EVENTS.append(event)
        append_jsonl(EVENTS_FILE, event)
        return jsonify({"ok": True, "event": event, "device": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/observations")
def record_observation():
    body = request.get_json(silent=True) or {}
    compartment = str(body.get("compartment_id", "")).upper()
    if compartment and compartment not in COMPARTMENTS:
        return jsonify({"ok": False, "error": "compartment_id must be A, B, C, or D"}), 400
    allowed = {"compartment_id", "state", "changed_pixel_fraction", "before_image", "after_image", "note"}
    obs = {k: v for k, v in body.items() if k in allowed}
    obs["timestamp"] = utc_now()
    obs["type"] = "VISION_OBSERVATION"
    RECENT_OBSERVATIONS.append(obs)
    del RECENT_OBSERVATIONS[:-100]
    append_jsonl(OBSERVATIONS_FILE, obs)
    return jsonify({"ok": True, "observation": obs}), 201


@app.get("/api/status")
def status():
    counts = {"PASS": 0, "FAIL": 0, "REMINDER": 0, "STOP_ALERT": 0, "DEVICE_ERROR": 0}
    for ev in RECENT_EVENTS:
        if ev.get("type") in counts:
            counts[ev["type"]] += 1
        if ev.get("status") == "DEVICE_ERROR":
            counts["DEVICE_ERROR"] += 1
    return jsonify({"ok": True, "timestamp": utc_now(), "device": DEVICE.get_state(), "counts": counts, "events": RECENT_EVENTS[-100:], "observations": RECENT_OBSERVATIONS[-100:]})


if __name__ == "__main__":
    print("MedVerify local server starting...")
    print("Device mode:", DEVICE.mode)
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
