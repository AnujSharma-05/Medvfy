# MedVerify — How the whole system works

This is the end-to-end picture: what each part does, how a dose flows through it,
what it can and cannot know, and how to test it on a laptop.

---

## 1. The idea in one paragraph

At a scheduled time the box lights the right compartment and beeps. A camera above
the box watches the compartments continuously. When the camera confirms that the
due compartment has been emptied, the dose is marked **REMOVED** and the alert
stops. If a different compartment is opened, the person is warned. If nothing
happens within the dose window (30 min by default), the dose is marked **MISSED**
and a caregiver is notified. Everything is shown on a local web console and can be
pushed to a phone.

> The camera sees the compartment, not the person. "Removed" means the medicine left
> the compartment. It is never reported as "swallowed".

---

## 2. The parts and who owns what

```
                ┌──────────────────────────── laptop ───────────────────────────────┐
 webcam ──────► │ vision/camera_inspector.py  (camera process, OpenCV window)       │
                │   ContinuousMonitor  → is the scene still? evaluate every 0.5 s   │
                │   VisualComparator   → how different from "loaded" / "empty"?     │
                │   CompartmentStateMachine → confirmed state per compartment       │
                │   EventLogger ──► CSV log + POST /api/observations (on change)    │
                │               └─► POST /api/vision/heartbeat (every 5 s)          │
                │                                   │                               │
                │ backend/app.py  (server process, http://127.0.0.1:5000)           │
                │   ScheduleStore  config/schedule.json  daily dose times           │
                │   DoseManager    starts doses, matches camera events, escalates   │
                │   Notifier       in-app feed (+ browser pop-ups) (+ phone push)   │
                │   Device adapter mock | ESP32 over USB serial | ESP32 over Wi-Fi  │
                │   Web console    backend/static/console.html                      │
                └───────────────────────────────────────────────────────────────────┘
                                         │ USB serial ("A", "STOP")
                                         ▼
                                ESP32: LEDs A–D + buzzer
```

**Why the split?** The camera process is a *sensor*: it only says what it sees. The
server owns *time and decisions*: schedule, dose sessions, alerts. That lets you test
either half alone (the fake camera tool drives the server; unit tests drive the
camera logic with synthetic frames), and a camera crash cannot silently corrupt the
schedule.

---

## 3. One dose, step by step

| Time  | What happens | Where |
|-------|--------------|-------|
| 08:00 | Scheduler tick (every 1 s) finds the 08:00 dose for **A** → opens a dose session | `DoseManager.tick` |
| 08:00 | LED **A** on + beep; "Time for your medicine — compartment A" notification | device adapter, notifier |
| 08:00 | Checks: is the camera reporting? Does A already look empty? (flags `CAMERA_OFFLINE`, `NOT_LOADED`) | `start_session` |
| 08:03 | Hand reaches in → frames differ → camera **pauses decisions** (scene moving) | `ContinuousMonitor` |
| 08:03 | Hand leaves → scene still for 1 s → evaluate: A differs from "loaded", matches "empty" → `REMOVAL_CANDIDATE` | state machine |
| 08:03 | Still empty for ≥ 2 s and ≥ 2 evaluations → `REMOVAL_DETECTED` → POST to server | state machine, EventLogger |
| 08:03 | Server: A went loaded → removed while A's dose is open → dose **REMOVED**, LED off | `DoseManager` |
| 08:05, 08:10, … | (if not taken) re-beep + "still waiting" notification every 5 min | `tick` |
| 08:30 | (if still not taken) dose **MISSED**, critical notification, LED off | `tick` |

Other outcomes and flags:

| Situation | Result |
|-----------|--------|
| Compartment C opened while A is due | warning "Wrong compartment: C"; light re-pointed at A; dose stays open; flag `WRONG_COMPARTMENT:C` |
| A changed but does not look empty | warning "only partly emptied?"; if never emptied → outcome `PARTIAL` |
| A looks full again within 10 min of removal | warning "Medicine put back?"; flag `PUT_BACK` on that dose |
| A looks full again later | info "Compartment A refilled" |
| A emptied when no dose is due | warning "emptied outside dose time" |
| Camera stops reporting during a dose | warning once; "restored" when it comes back |
| Box hardware unreachable | warning; dose continues with on-screen/phone reminders (`DEVICE_ERROR`) |
| Two doses for the same compartment overlap | the later one is recorded `SKIPPED`; the open one continues |
| "Stop alert" pressed | open doses closed as `CANCELLED` |

The server acts only on **confirmed** states (`EXPECTED_PRESENT`, `OBJECT_RETURNED`,
`REMOVAL_DETECTED`, `EMPTY`, `PARTIAL_OR_WRONG`) and only when a compartment changes
**category** (loaded / removed / changed). Candidates and `UNCERTAIN` are displayed but
never acted on, and duplicate reports or heartbeats can never close a dose twice.

---

## 4. How the camera decides (summary)

1. **Calibration (manual, once per loading):** `R` draw boxes A–D → `S` photo with
   compartments loaded ("expected") → `E` photo with them empty ("empty").
2. **Stability gate:** compare each frame with the previous one; >2 % of pixels in any
   compartment changed = moving → no decision, pending confirmations restart.
3. **Evaluation (every 0.5 s once still for 1 s):** for each compartment, a 0–1
   difference score against "expected" and against "empty" (SSIM + pixel diff + blob count).
4. **State machine:** < 0.05 from expected = loaded; ≥ 0.08 from expected and < 0.05 from
   empty = removed (candidate first); changed but not empty = partial/wrong.
5. **Confirmation:** a removal/return must persist for ≥ 2 evaluations **and** ≥ 2 s.
6. **Reporting:** only state *changes* are logged/sent; a heartbeat every 5 s carries
   liveness + a snapshot (which also recovers a lost POST).

All of these numbers live in `config/settings.json`.

---

## 5. Flaws in the flow (and what is done about each)

| # | Flaw | Status |
|---|------|--------|
| 1 | **Removal ≠ ingestion.** The camera cannot see swallowing. | Inherent. Wording everywhere says "removed". |
| 2 | **Laptop sleep stops everything** (scheduler, camera). A sleeping laptop = no reminders. | Not solvable in software. Set Windows power to *never sleep when plugged in*; long-term, run on an always-on device (Raspberry Pi / mini PC). |
| 3 | **Camera process must be running and monitoring.** If its window is closed, nothing is verified. | Mitigated: heartbeat → `CAMERA_OFFLINE` warning at dose time and during a dose. Not alerted *between* doses. |
| 4 | **Calibration trusts the operator.** Whatever is in the box when `S` is pressed becomes "correct". | Procedural: load the box correctly, then press `S`. |
| 5 | **Refill detection relies on looks.** A refilled compartment is recognised as loaded only if the new pills look like the calibrated ones. Different pills → stays "removed" → next dose flagged `NOT_LOADED`. | Procedural: after loading *different* pills, press `S` again. |
| 6 | **Thresholds are not yet tuned on real images.** The blob-count boost (≥ 0.15 on any count change) can over-react to compartment texture/reflections. | Needs real-camera sessions with ground truth (Milestone 3). |
| 7 | **`PARTIAL_OR_WRONG` is confirmed on one observation** (no time confirmation), so a still hand inside a compartment can be read as "changed". | Mitigated by the motion gate and because the server only *flags* it (dose stays open). Candidate for time confirmation. |
| 8 | **Blur check used a fixed sharpness threshold (60).** Measured on the real webcam: sharpness 13–22, so every frame would have been "blurry". | Fixed: blur is now judged relative to the sharpness recorded at `S` (`blur_min_ratio`, default 0.5). Recalibrate with `S` to record it. The overlay shows `sharp X/limit`. |
| 9 | **Firmware lights one LED at a time.** Two compartments due together → light shows the latest; console/phone list both. | Firmware change later (hardware task). |
| 10 | **Firmware beeps once per command**, not continuously. | Server re-beeps every 5 min. Continuous buzzing = firmware change. |
| 11 | **No acknowledge button on the box.** Stopping the alert needs the console. | Hardware task (button on ESP32). |
| 12 | **Doses that fell entirely while the system was off are not recorded.** A dose >30 min late at start-up is skipped silently. | Known gap; record as "not monitored" in a later iteration. |
| 13 | **Phone push via ntfy leaves the laptop.** The topic name is the only secret. | Off by default; use a long random topic; put it in an environment variable, not the committed settings file. |
| 14 | **Console has no login.** | Bound to `127.0.0.1` only, so only this laptop can open it. A phone/web app will need auth. |

---

## 6. Notifications — setup

**Always on:** the console's notification feed (also saved to `data/notifications.jsonl`).

**Desktop pop-ups:** open the console → *Enable pop-ups* → allow. They appear while the
console tab is open (it can be in the background).

**Phone push (optional, free, no account) via [ntfy](https://ntfy.sh):**
1. Install the *ntfy* app (Android / iOS).
2. Pick a long random topic, e.g. `medverify-7f3k9q2xw8` (anyone who knows it can read it).
3. In the app: *Subscribe to topic* → enter it.
4. Start the server with the topic in an environment variable (PowerShell):
   ```powershell
   $env:MEDVERIFY_NTFY_TOPIC = "medverify-7f3k9q2xw8"
   .\.venv\Scripts\python.exe -m backend.app
   ```
   (or set `notifications.ntfy.enabled: true` and `topic` in `config/settings.json`).
5. Console → *Send test notification*. The feed shows `ntfy: delivered` or the error.

Levels map to phone priority: info → default, warning → high, critical (missed dose) → urgent.

---

## 7. Hardware modes

`config/settings.json` → `device_mode` (or env `MEDVERIFY_DEVICE`):

| Mode | Use | Needs |
|------|-----|-------|
| `mock` | Laptop only (default). Prints what the box would do. | nothing |
| `serial` | ESP32 with the existing `MedVerify_Controller.ino`, over USB | `serial_port` (e.g. `COM5`) or env `MEDVERIFY_SERIAL_PORT` |
| `esp32` | Future Wi-Fi firmware (`POST /command`) | firmware not written yet |

If serial is selected but misconfigured, the server falls back to mock and posts a warning.

---

## 8. Running it

```powershell
.\run_medverify.bat        # server + browser console + camera window
.\run_server.bat           # server only
.\run_fake_camera.bat      # keyboard-driven fake camera (no webcam)
.\run_tests.bat            # full automated suite
```

Camera window keys: `R` regions · `S` save loaded · `E` save empty · `M` monitoring on/off ·
`V` one manual check · `C` switch camera · `Q` quit. Monitoring starts automatically when
calibration exists (`monitor_on_start`).

---

## 9. Tonight's laptop test (≈ 30 minutes)

**Part A — server + notifications, no camera (10 min)**
1. `run_server.bat`, open http://127.0.0.1:5000. Chips: camera *offline*, box *mock*.
2. *Enable pop-ups*, *Send test notification* → feed + pop-up.
3. `run_fake_camera.bat`. Camera chip turns green within 5 s.
4. Console: *Start a dose now* for A. Expect: "Right now: Take from compartment A", pop-up.
5. Fake camera: `C removed` → "Wrong compartment: C" warning.
6. Fake camera: `A removed` → dose **REMOVED** in history, alert stops.
7. Fake camera: `A loaded` within 10 min → "put back?" warning.
8. Add a schedule entry for the next minute on B → when the minute arrives the dose opens by itself.
9. Fake camera: `off` → wait 30 s → "Camera stopped monitoring". `on` → "restored".
10. *Stop alert* → dose `CANCELLED`.

**Part B — real camera with dummy objects (20 min)**
1. Close the fake camera. `run_medverify.bat`.
2. Your old calibration has every compartment "removed" in `data/vision_state.json`, so
   recalibrate in this order: `R` (redraw boxes; clears old references and stops
   monitoring) → empty all compartments → `E` → load all with dummy objects → `S`
   (marks all "loaded"). Loading last means monitoring never sees a fake removal.
3. Press `M` to start monitoring (it also starts automatically on the next launch).
   Banner shows `[MONITOR …]`; console camera chip green.
4. Start a dose for A from the console. Reach in, remove A's object, withdraw your hand.
   Expect within ~4 s: REMOVED.
5. For measuring accuracy, use trial mode (section 10) instead of free-form notes: it records
   ground truth and scores together so the thresholds can be tuned from them.

---

## 10. Trial mode: teaching the algorithm with real images

Trial mode measures the camera pipeline on labelled experiments. Nothing in it learns
automatically: you record trials, the evaluator replays them under many threshold
settings, and you choose (or `--apply`) the best one. Trials use a separate state that is
never saved, send nothing to the dose engine or the son, and the console shows
"Camera in trial mode".

**Keys (camera window):** `T` enter/leave trial mode · `1`–`4` cycle what you will do to
A–D: *unchanged → removed → changed* · `N` start a trial · `F` finish it.
Labels stay set between trials, so repeating the same trial is just `N` … `F` again.

**One trial:**
1. Box set up exactly as calibrated (loaded with your 4 objects).
2. Set the labels with `1`–`4` (shown on each box as `will: removed`).
3. Press `N`, take your hands away. Banner: *checking the box matches calibration…*
4. Banner says **GO** → do the action, take your hand fully out of view.
5. Wait until the states settle (about 3–4 s after your hand leaves), then press `F`.
   The terminal prints a per-compartment verdict. If the banner says a compartment *does
   not match calibration*, fix the box and press `N` again: that trial is excluded.
6. Put the objects back as calibrated for the next trial.

**Calibration for your setup (page background + 4 objects):** `R` draw A–D →
remove all objects → `E` → place the 4 objects → `S`. Recalibrate (`E` + `S`) whenever
you change the objects, the page, the light, or move the camera. `S` also records how sharp
the scene is; the terminal prints it.

**Before the first trial, check the banner:** `[TRIAL … sharp 18/9]` means the current
frame scores 18 and frames below 9 count as blurry. If the first number keeps dipping under
the second while nothing moves, compartments will flip to `UNCERTAIN`: lower
`blur_min_ratio` in `config/settings.json` (e.g. 0.35). `DARK` in the banner means too little
light: turn on a lamp. If the camera image is pure black, check the privacy shutter or that
no other app (Teams, Camera) is using the webcam.

**Suggested first session (~40 trials, about 30 minutes):**

| Trials | What to do | Labels |
|---|---|---|
| 8 | Nothing (hand in and out without touching) | all unchanged |
| 8 | Remove one object (A, B, C, D twice each) | that one removed |
| 4 | Remove two objects at once | both removed |
| 4 | Swap two objects | both changed |
| 4 | Put a different object in one compartment | that one changed |
| 4 | Nudge one object a little (no removal) | all unchanged |
| 4 | Remove one, put it back within the trial | all unchanged |
| 4 | Remove one while room light changes (lamp on/off) | that one removed |

**Afterwards:** run `run_evaluate.bat`. The report (also saved as `data/trials/report.md`)
shows accuracy with the current thresholds, the confusion table (what you did vs what the
camera said), whether the scores for "removed" and "unchanged" overlap at all, and the
best threshold settings. `run_evaluate.bat --apply` writes the best ones to
`config/settings.json`; `--rescore` recomputes scores from the saved frames, which is how
a future comparator change gets measured on the same footage.

Data per trial lives in `data/trials/<time>_<labels>/`: `trial.json` (labels, verdicts,
settings), `evaluations.jsonl` (every score), `frames/` (each evaluated frame),
`references.npz` (the calibration photos in force).
