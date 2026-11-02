# MedVerify — Test plan: hardware, detection depth, and the live demo

This plan covers the four goals of the depth phase:

1. the hardware works together with the software;
2. detection is robust to how people really handle medicine (taking out, putting back, crowded compartments);
3. a sensible amount of medicine per compartment (a supply, not one pill);
4. all of it shown live, end to end.

Each goal has **automated tests** (run any time with `.\mv test`) and **live checks** (with your hands, `.\mv demo`).

---

## The model: one compartment = one medicine's supply, a dose = pills taken out

Putting a single pill in a big box is pointless: it is just repackaging. So a compartment holds
**one medicine's supply** (for example a week), and a dose is verified when the camera counts
**the right number of pills leaving it**:

| Camera sees during A's dose | Result |
|---|---|
| A's count drops by the dose size (e.g. 7 → 6) | **Taken**: LED off, "6 left" |
| A drops by more (7 → 5) | **Taken + "too many pills" (urgent alert)** |
| A drops by less (for a 2-pill dose) | waits, "1 of 2 taken so far" |
| Another compartment drops | "wrong compartment"; light points back at A |
| A's count goes back up | "**put back**: the dose may not have been taken" |
| Any count reaches 2 | "**refill soon**" |

A compartment calibrated with exactly the dose (e.g. 1 pill) still works: the count drops to 0.

### How the camera counts
It measures the **area of medicine** in each compartment, as the difference from the empty
photo. It divides that by the area of one pill, which it learns at calibration from the count
you type. Area does not change when pills are moved, turned, touch each other, or are put
back somewhere else, and an overall light change is evened out. A new count is accepted only
when it has held for 2 s over 2+ checks with nothing moving in view.

### Physical rules for a fair test (and for real use)
- **One kind of pill per compartment, all the same size.** For the paper model, cut equal pieces.
- **Single layer.** A pill lying on another hides it, so it gets undercounted (tested: a known limit).
- **Up to about 10 per compartment** with this webcam at 640×480; one pill must stay clearly visible.
- **Medicine fully inside its region, paper around it.** Nothing else in the regions (LEDs, wires).
- **Even light, fixed camera.** If either changes a lot, recalibrate (`.\mv cam empty`, `.\mv cam loaded …`).
- **Not white-on-white:** a white pill on white paper is nearly invisible. Use a coloured tray then.

---

## 1. Hardware with software

| Check | How | Kind |
|---|---|---|
| Board answers, auto-found port, resend after reconnect, silent board = error | `test_notifier_schedule_device.py` | automated |
| Guided wiring check (on-board LED vs breadboard LEDs vs buzzer) | `.\mv box diag` | live |
| Firmware build + flash without the Arduino IDE | `.\mv firmware build`, `.\mv firmware upload` | live (done 23 Oct) |
| Dose lights the right LED and turns it off when taken | demo **S1**, **S3** | live |

## 2. Detection robustness

Automated on **real textures** (the paper of the test sheet and a real strip cut from the
webcam photo), in `tests/test_counting.py`:

| Scenario | Expected | Result |
|---|---|---|
| 5 strips untouched | 5 | pass |
| one / two taken | 4 / 3 | pass |
| rearranged, or put back elsewhere | 5 | pass |
| strips touching each other (one blob) | 5 | pass |
| 10 strips packed densely, then one taken | 10 → 9 | pass |
| strips rotated 90° | unchanged | pass |
| room light −20% / +15% | unchanged | pass |
| camera noise | unchanged | pass |
| two strips stacked on others | **undercounted** (known limit) | documented |

Plus, in `tests/test_camera_service.py`, the whole camera service on a full frame: calibrate with
counts, take one strip out of A → confirmed **5 → 4** and reported; rearranging B → no event.
Region finding is checked on both real photos (straight and rotated camera, stand/desk ignored).

Live (demo): **S2** untouched box (no false alarms), **S4** taken and put back, **S7** rearranging,
**S8** a hand resting over a compartment.

## 3. Dose logic with counts

`tests/test_dose_manager.py` (`TestCountedDoses`): exact dose, too many, extra pill right after,
2-pill dose in steps, put back during and after a dose, wrong compartment, not enough pills,
refill soon (alerted once), refill and recalibration stay silent, area-based states never
override counts, schedule's pills-per-dose reaches the dose.

## 4. The live demo

```powershell
.\mv up
.\mv cam regions preview          # 4 groups listed?
.\mv cam regions auto A=1 B=3 C=2 D=4
.\mv cam empty                    # sheet cleared, hands away
.\mv cam loaded A=4 B=4 C=4 D=4   # sheet loaded; the REAL number of pieces in each
.\mv demo                         # or: .\mv demo S3 S6
```

| Id | Scenario | You do | Pass when |
|---|---|---|---|
| S1 | Box | watch 12 s | you confirm all LEDs, blue LED, tone |
| S2 | Untouched box | nothing for 20 s | no count change, no alert |
| S3 | Dose taken | take 1 from A | dose closes as taken, LED A on then off |
| S4 | Taken and put back | take 1 from B, put it back anywhere | not accepted as a dose without a "put back" flag |
| S5 | Wrong compartment | take from D during C's dose, then from C | warning first, dose accepted after |
| S6 | Too many | take 2 from A (dose 1) | "too many pills" alert |
| S7 | Rearranging | slide pieces inside B | no change |
| S8 | Hand resting | hold a hand over C for 5 s | no change |

The demo puts everything back between scenarios (it asks you to), prints a pass/fail table with
reaction times, and saves it to `data/demo/`. It is itself tested end to end against the real
server with a simulated camera (`tests/test_demo.py`): all scenarios pass with a camera that
sees correctly, and the dose scenarios fail with a camera that sees nothing.

## Known limits (honest list)

- Stacked pills are undercounted. Single layer only.
- Pills of different sizes in one compartment make counts approximate.
- White pills on white paper are nearly invisible.
- The camera sees pills leave the compartment, never being swallowed.
- Counts are only as good as the declared count at calibration: count the pieces carefully.
