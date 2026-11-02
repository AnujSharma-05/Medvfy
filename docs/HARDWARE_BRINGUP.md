# MedVerify — Running the real setup from VS Code

Everything is controlled from the VS Code terminal with **`.\mv`** (or *Terminal → Run Task →
MedVerify: …*). No Arduino IDE and no camera window are needed. Wiring is in
[HARDWARE_CONNECTIONS.md](HARDWARE_CONNECTIONS.md).

```
.\mv up / down / status / logs        start, stop, check everything
.\mv box check | test | light A | off pill box (ESP32 LEDs + buzzer over USB)
.\mv cam regions preview | auto        find the 4 compartments on the sheet
.\mv cam empty | loaded | monitor on   calibrate and watch
.\mv dose start A | cancel             open / cancel a dose
.\mv trial start A=removed | finish | eval    labelled tests for tuning
.\mv firmware build | upload           (re)flash the ESP32 from the terminal
```

## Verified on 23 Oct 2026

| Link | Result |
|---|---|
| ESP32 on USB | Found automatically as COM28 (CP210x); `serial_port: "auto"` follows it if the number changes |
| Firmware toolchain | `mv firmware build` and `mv firmware upload` work with the Arduino IDE's bundled arduino-cli (esp32 core 3.3.12); board answers after flashing |
| Box via server | `mv box test`: the firmware acknowledged A, B, C, D and all-off |
| Camera | Camera 0 delivers 640×480; settings now match |
| Region finding | On the photo of the test sheet: 4 groups found, A blue (top-left), B dark (top-right), C green (bottom-left), D purple (bottom-right) |
| Pipeline on the real photo (automated test) | Calibrate → take away A's strips → only A becomes REMOVAL_DETECTED; a trial scores 4/4 |

**Not yet verified (needs your hands or eyes):** that each LED physically sits next to the
compartment its letter maps to, the camera framing the whole sheet, and real removals.

## Steps

1. **Place the camera** so it sees the whole sheet like the reference photo: all four groups
   in view, nothing else moving, even light. Fix it in place (tape); any later movement means
   recalibrating.
2. `.\mv up` — the webcam turns on and stays on until `.\mv down` (or `.\mv down camera`).
3. `.\mv cam regions preview` — lists the groups on the sheet, numbered, with their colours,
   and saves `data\snapshots\regions_preview.jpg` (open it in VS Code). Anything running off the
   edge of the picture (desk, stand, hand) is ignored. Adjust the camera until it lists **4 groups**.
   Nothing is changed by a preview.
4. `.\mv cam regions auto A=1 B=3 C=2 D=4` — gives each compartment letter the preview number of
   its group, so letters follow the LEDs even if the camera is rotated. (Without numbers: the 4
   largest groups in reading order.) This clears any old calibration.
   *Current setup (23 Oct): preview 1 blue, 2 green, 3 dark, 4 purple → A blue, B dark, C green, D purple.*
5. Take **all** medicine off the sheet, hands away: `.\mv cam empty`.
6. Put everything back exactly, hands away: `.\mv cam loaded` — monitoring starts.
7. `.\mv status` — expect `Boxes: A=EXPECTED_PRESENT … D=EXPECTED_PRESENT` and sharpness above its limit.
8. **LED ↔ compartment check:** `.\mv box light A` must light the LED next to the **top-left**
   group (then B top-right, C bottom-left, D bottom-right). `.\mv box off` afterwards. If the
   order is different, either move the LEDs or tell me and I will remap the letters.
9. **LEDs must stay outside the regions** drawn in the preview, or their light looks like a
   change: `.\mv dose start A` with hands off, wait 10 s, `.\mv status` must still show all
   compartments EXPECTED_PRESENT. `.\mv dose cancel`.
10. **Full loop:** `.\mv dose start A`, take the blue strips away, move your hand out, wait ~4 s:
    `.\mv status` shows the dose gone, A = REMOVAL_DETECTED, LED off. Put them back and run
    `.\mv cam loaded` to re-arm.
11. **Tuning trials:** `.\mv trial start A=removed` → wait for **GO** → do it → `.\mv trial finish`.
    Repeat with other compartments, swaps (`=changed`), and no-change trials. Then `.\mv trial eval`.

## When something is off

| Symptom | Check |
|---|---|
| `mv up` says port in use | Another server window is open: `.\mv down --force` |
| Pill box ERROR in status | Arduino Serial Monitor open? USB unplugged? `.\mv box check` |
| Camera not reporting | Camera app or Teams holding the webcam; `.\mv logs camera` |
| Sharpness below its limit | Camera moved or out of focus since `cam loaded`: fix it, re-run steps 5–6 |
| Fewer than 4 groups found | A group is out of view, too close to another, or white-on-white |
| `firmware upload` fails to connect | Hold the board's BOOT button while it prints "Connecting…" |

## Firmware limits (unchanged)

One LED at a time · 3 beeps per command, not continuous · no button on the box · USB only.
