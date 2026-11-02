# MedVerify: handoff (state on 2 Nov 2026)

Read this first in a new chat, then `docs/HOW_IT_WORKS.md` and `README.md`.

## What the project is
A pill box for an elderly patient. Four compartments (A-D) are pen-drawn squares on a white
sheet; coloured paper pieces stand in for medicine. A webcam above watches continuously and
COUNTS the pieces in each square. An ESP32 lights the due compartment's LED and sounds a
buzzer. Three web apps on one server (port 5000):
- admin console `http://127.0.0.1:5000/console`
- patient (father) screen `http://127.0.0.1:5000/patient` (English + Hindi)
- caretaker (son) app `http://127.0.0.1:5000/care` (password; port 5050 is only for a future public tunnel)

The camera sees removal only, never swallowing: never claim ingestion.

## How to run (everything from VS Code, never the Arduino IDE)
- One command demo: `bash start_demo.sh` (simulated box) or `bash start_demo.sh hardware`.
  It stops leftovers, starts server + camera, opens the 3 apps, offers camera set-up, runs the
  guided demo.
- PowerShell: `.\mv up` / `.\mv down` / `.\mv status` / `.\mv help`. Do NOT use
  `run_medverify.bat` (starts duplicate processes).
- Simulated box: `$env:MEDVERIFY_DEVICE="mock"` before `.\mv up`. `config/settings.json`
  keeps `device_mode: serial`, `serial_port: auto` (ESP32 = CP210x, was COM28).
- Firmware: `.\mv firmware upload` (arduino-cli). Box checks: `.\mv box check|test|diag|alarm|warn`.
- Tests: `.\mv test` (261 pass).

## Camera set-up (whenever the camera or sheet moves; sheet must be taped down)
1. `.\mv cam regions preview` - must list 4 squares, numbered A=1 top-left, B=2 top-right,
   C=3 bottom-left, D=4 bottom-right. Faint pen lines are not found: darken them.
2. `.\mv cam regions auto`
3. Sheet EMPTY: `.\mv cam empty`
4. Pieces in: `.\mv cam loaded A=4 B=4 C=4 D=3` (real counts; starts monitoring)
Ask the user before every webcam picture (they asked to be told).

## Dose logic (backend/dose_manager.py)
A dose = the count dropping by the dose size. Alerts: TOO_MANY (critical: urgent ALARM
siren on the box + red "Stop" on the patient screen), WRONG_COMPARTMENT (screen names the
right box; light moves back and beeps), MULTI_COMPARTMENT (2+ boxes within 20 s: "one
medicine at a time" + WARN beeps), PARTIAL, PUT_BACK, UNSCHEDULED_REMOVAL, LOW_STOCK.
Patient alerts stay 90 s or until OK. Start a test dose: `.\mv dose start A pills=1`.

## Vision (vision/)
- `drawn_regions.py`: finds pen squares (tilt <= 15 deg, gaps); compartment = the square's
  exact outline (saved in config/rois.json "outlines"); crop box = outline + 12 px margin.
- `content.py`: medicine = difference from the EMPTY photo; count = area / one-piece area
  (learned from the declared count). Hand guard = an object crossing the pen line
  (`crosses_line`). Count up needs a reading within 0.2 of a whole number, down 0.35.
- Crumpled pieces change visible area (up to ~25%); flat cut pieces count reliably.

## Status
Done and committed (branch feat/drawn-compartments, PR #3 open against main): drawn squares,
scattered pieces, hand guard, sheet-slide follow, patient alerts + buzzer patterns, real ESP32
flashed and tested (LEDs A-D, alarm, warn all confirmed working), live calibration on the real
setup (A 4, B 4, C 4, D 3).

Live demo launcher: `live_demo.bat` (real box; `live_demo.bat mock` for simulated). Starts
everything, opens the 3 apps, leaves a command window with the demo controls. No scripted tests.

### Demo flow for the judges (about 5 minutes)
1. Show the 3 apps and `mv status` (counts per box match the sheet).
2. Normal dose: `mv dose start A pills=1` -> LED A + beeps, patient screen "box A". Take 1 piece
   from A, hand away -> after ~3 s screen shows "Done", caretaker app logs it, LED off.
3. Too many: `mv dose start B pills=1`, take 2 from B -> siren + red "Stop" + critical alert.
4. Wrong box: `mv dose start C pills=1`, take from D -> "Wrong box, take from C", LED C beeps.
   Then take from C -> dose accepted.
5. One at a time: take from two boxes together -> "one medicine at a time" + two beeps.
6. Hand resting on a box -> no false removal (view blocked).
Put pieces back between steps and run `mv cam loaded ...` with the real counts if needed.

### Last guided run (`mv demo`, real box, 2 Nov 17:24): 5/8 passed
FAILED: S3 (take 1 from A), S5 (wrong compartment then right one), S6 (take 2 instead of 1).
Passed: S1 box, S2 no false alarms, S4 put back, S7 rearranging, S8 hand resting.
The three failures are all "counted removal during a dose". Details: `data/demo/20261102-172418.json`.
FIRST THING TO DO in a new chat: read that JSON (what count/state the camera reported vs
expected). Likely causes to check, in order: (a) the demo's timeout is shorter than count
confirmation (2 observations + 2 s after the scene is still, plus 1 s settle); (b) small grid
far from camera -> one piece ~600-850 px, crumpled pieces change area, estimate not near a
whole number so `CountTracker` never confirms (see `mv status` "camera estimates" while
taking a piece); (c) hands not out of view long enough. Fix, add a test, run `mv test`.

Remaining:
- Full hardware run: `.\mv demo` with the real box (each scenario: normal dose, too many,
  wrong box, two boxes at once), watching the 3 apps.
- Merge PR #3 after that run.

## Diagnosed 2 Nov 17:55: "LED stays on, screen does not update" in the live demo
Cause: the camera or sheet moved after calibration (while wiring the hardware). camera.log:
recalibrate refused for A, B, C, D at once ("lies across the drawn line") and no count reports
afterwards: every box reads "view blocked", counts freeze, doses never close. Not a hardware
bug. Fix: fix camera + tape sheet, then redo set-up (regions auto -> empty -> loaded).
DONE (commit after 7bb10e7): ALL boxes blocked for > 10 s now raises a critical CAMERA_MOVED alert
(caretaker + patient screen) instead of silently freezing. Reports: docs/report/ (build_report.js).

## 2 Nov 23:30: speed + crumpled pieces + guided set-up
- `config/tuning.json` (read by the camera service on top of settings.json): settle 1 s + confirm
  1 s (was 1 + 2, about 3 s -> about 2 s), count_tolerance 0.45 for decreases, unit_follow 0.1 (one pill's
  area follows crumpled pieces while the count is stable, capped at +/-30%). Delete the file to undo.
- `setup_camera.bat`: guided regions -> empty -> loaded, backs up the old calibration to
  data/calibration_backup/<time>/ first; `restore_camera.bat <time>` puts one back.
- Found at 23:25: the calibration from 17:53 no longer matched (camera/sheet moved; all boxes
  "view blocked"). Squares are found fine in the new position; set-up must be redone by the user.
- Demo script: docs/DEMO_SCRIPT.md. Evaluator questions: docs/EVALUATOR_QA.md.
- 23:45: set-up redone (regions auto, empty, loaded A=4 B=4 C=4 D=3); estimates 4.09/4.05/4.06/2.98, no box blocked.

## 2 Nov 23:50-00:10: why doses were not detected (fixed)
- Counting gate saw glinting crumpled foil as motion (20% of still frames > 2%), so counts were
  never confirmed (A read 3.0, stayed 4). Fix: smooth before differencing (vision/frame_quality.py).
- Restarting the camera service reset counts to the set-up values (false PUT_BACK). Fix:
  resume_counts() takes the server's counts at start.
- Webcam replug resets auto exposure/white balance: estimates drift 0.3-0.6. Colour cast is now
  evened out; after a replug, retake only the loaded photo (`mv cam loaded ...`).
- software_demo.bat = live_demo.bat with a simulated box (same software path).
- Webcam hung once when reopened quickly by two programs: unplug/replug fixed it.
- 23:55 live check (simulated box, real camera): correct dose A closed in ~3 s; dose B take-2 ->
  TOO_MANY critical + red Stop screen; pieces taken from C during a B dose -> WRONG_COMPARTMENT screen.
  config/tuning.json had been invalid JSON (silently ignored) until c2a92d3: tuning is now really live.

## 3 Nov: frozen for the demo
Tag `demo-working-2026-11-02` = working state. New README (story, flow maps, structure; no files
moved), old README kept as docs/TECHNICAL_REFERENCE.md, viva guide docs/VIVA_PREP.md.
