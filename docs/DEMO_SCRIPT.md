# MedVerify: demonstration script (about 7 minutes)

Two presenters: **P1** speaks and explains; **P2** handles the pill box and types commands in the
`live_demo.bat` window. Keep hands out of the camera view except when "taking" a pill.

## Before the evaluator arrives (10 minutes earlier)
1. Tape the sheet down, and fix the camera so neither can move. A moved camera or sheet is the
   one thing that breaks the demo.
2. Plug in the ESP32 (USB). Double-click `live_demo.bat`. Three browser tabs open: console, patient, caretaker.
3. If the camera or sheet moved since the last set-up: run `setup_camera.bat` (a guided 2-minute set-up).
4. After the camera has been on for a minute: `mv cam loaded A=4 B=4 C=4 D=3` (real counts), so
   the photo matches tonight's exposure.
5. `mv status` must show every box as `A=n/n` with **no** "view blocked" line.
6. Log in to the caretaker tab. Put the patient tab on the second screen, if there is one.
7. Refill counts to what is written in `mv status` before every new evaluator.

## 1. The problem (P1, 45 s)
> "Elderly patients often forget doses, take a double dose, or take from the wrong box. Families
> want to know without calling every few hours. Existing smart pill boxes only know a lid was
> opened, not whether the right number of pills left the right compartment."

Point at the box: *four compartments A to D, the ESP32 with one LED per compartment and a buzzer,
and a webcam above that counts what is inside each compartment.*

## 2. The three users (P1, 45 s)
Show each tab briefly:
- **Admin console**: system health, camera, box, live counts.
- **Caretaker app**: password-protected, sets the schedule, sees history and alerts.
- **Patient screen**: large text in English and Hindi, one instruction at a time.

> "One Flask server runs all three. The camera service and the ESP32 talk only to that server."

## 3. A correct dose (P2 acts, P1 narrates, 60 s)
- P2 types `mv dose start A pills=1`.
- **LED A lights and the buzzer beeps**; the patient screen says *take 1 from box A*.
- P2 takes one piece from A, then moves the hand away.
- About 2 seconds later: **LED off, buzzer stops**, the patient screen says *Done*, and the caretaker app logs *taken*.

> "The camera judges only when the scene is still, so a moving hand is never counted. It measures
> the medicine area in each drawn square against a photo of the empty sheet, and divides by the
> area of one pill learned at set-up."

## 4. Too many pills (60 s)
- P2: `mv dose start B pills=1`, then takes **two** pieces from B.
- The result is a **siren** on the box, a red *Stop, put the extra back* message on the patient screen, and a **critical** alert in the caretaker app.
- P2 puts one piece back. The count goes back up and the alert can be cleared with OK.

## 5. Wrong compartment (60 s)
- P2: `mv dose start C pills=1`, then takes one piece from **D**.
- The patient screen says *Not D, take from box C*, and **LED C beeps again**.
- P2 puts the D piece back, then takes from C. The dose is accepted.

## 6. Two boxes at once (30 s)
- With no dose open, P2 takes one piece from A and one from B together.
- The box gives a double beep (WARN), the screen says *take one medicine at a time*, and the caretaker is informed.

## 7. Safety guards (45 s)
- P2 rests a hand over box D for a few seconds. **Nothing is counted** (the view is blocked).
- P1: *"If the camera or sheet is moved, every box is blocked; after 10 seconds the caretaker
  gets a critical 'camera moved' alert instead of the system silently stopping."*
- P1: *"We never claim the pill was swallowed: the camera sees removal from the box only."*

## 8. Close (30 s)
> "Next: Wi-Fi firmware so the ESP32 needs no USB cable, phone push notifications, and a
> 3D-printed box with fixed compartments in place of the paper sheet."

Then `mv down` when the session ends.

## If something goes wrong
| You see | Do |
|---|---|
| `mv status` shows "view blocked: A, B, C, D" | Camera or sheet moved: `setup_camera.bat` |
| Count shows `?`, or a dose does not close after 10 s | Hands fully out of view; wait 3 s; check `mv status` camera estimates |
| No LED or buzzer | `mv box check`; replug the USB, then `mv down` and `live_demo.bat` |
| Counts differ from the sheet after refilling | `mv cam loaded A=4 B=4 C=4 D=3` (the real counts) |
| Webcam replugged or restarted, and estimates are off by 0.3-0.6 | Wait 1 min, hands away, then `mv cam loaded A=.. B=.. C=.. D=..` with the real counts (auto exposure changed) |
| Set-up went wrong | `restore_camera.bat` (lists saved calibrations) |
