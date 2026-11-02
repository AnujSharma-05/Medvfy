# MedVerify: questions an evaluator may ask

> Short, recitable answers (including general IoT questions): [VIVA_PREP.md](VIVA_PREP.md).

## Concept
**1. What problem does it solve?**
Elderly patients miss doses, double-dose, or take the wrong medicine. MedVerify reminds the
patient (LED, buzzer and screen), then **verifies** with a camera that the right number of pills left
the right compartment. It alerts the patient at once and informs the caretaker remotely.

**2. How is this different from a normal smart pill box?**
Most boxes detect a lid opening (a switch) and assume the dose was taken. We count the pills, so we
can tell a correct dose from too many, too few, the wrong compartment, or pills taken and put back.

**3. Does it prove the patient swallowed the pill?**
No, and we say so clearly. The camera sees removal from the box only. Confirming ingestion would
need a different method, such as video of the patient or a smart blister pack, with privacy costs.

**4. Who are the three users?**
- Admin: set-up, system health, live counts.
- Caretaker: schedule, history, alerts; password login.
- Patient: large bilingual screen with one instruction at a time.

## Vision
**5. How does the camera count pills?**
1. At set-up we take a photo of the empty sheet and a photo with known counts.
2. Medicine pixels are what differ from the empty photo, in colour (Lab a/b) or brightness.
3. Count = medicine area ÷ the area of one pill (learned at set-up as area ÷ declared count).

Area does not change when pills are moved around or touch each other, which counting blobs cannot handle.

**6. Why not a trained model such as YOLO?**
- We have no labelled dataset of these pills.
- It would need a GPU or cloud.
- A classifier still must count overlapping pieces.

Area measurement is explainable, runs on a laptop in milliseconds, and needs one calibration photo. A detector is a natural upgrade for real pill shapes.

**7. How are the compartments found?**
They are pen-drawn squares. We enhance dark lines (black-hat filter), detect line segments at
angles up to ±15°, and close small gaps. Each closed white region not touching the frame edge
becomes a compartment. Its exact outline is stored, so pieces anywhere inside count, even at the line.

**8. What if a hand is in the frame?**
Two guards:
- Scene stability: nothing is judged while the image is changing. It must be still for 1 s.
- Line crossing: an object lying on both sides of a square's pen line is a hand or sleeve reaching in, so that box is marked "view blocked" and no count is taken from it.

**9. How do you avoid false alarms?**
- A new count must hold for 2 consecutive readings over at least 1 s.
- The estimate must be near a whole number.
- A count going **up** (pills put back) must be even closer (within 0.2), because a false increase would hide a dose that was really taken.

**10. Your pieces are crumpled. Doesn't their area change?**
Yes, by up to about 25%. Two measures handle this:
- Decreases are accepted within ±0.45 of a whole number.
- While a count is stable, the area of one piece slowly follows what is really in the box. It is limited to ±30% of the calibrated value and never learns from a reading that looks like a removal.

Flat, equal-sized pills (real tablets) count more precisely.

**11. What about lighting changes?**
Brightness is normalised to the paper (median) before comparing. A frame that is too dark, too
bright or blurred is rejected rather than judged.

**12. What if the camera or sheet moves?**
- Small slides (up to 40 px) are re-aligned automatically using the drawn lines.
- A bigger move makes every box look blocked. After 10 s this raises a critical "camera moved" alert to the caretaker and patient screen.
- Re-set-up takes about 2 minutes (`setup_camera.bat`).

**13. How fast is it?**
About 2 s after the hand leaves: 1 s of stillness, then 1 s of confirmation. The camera is checked every 0.5 s.

**14. What are the limits?**
- Pills must lie in a single layer.
- One kind of pill per compartment.
- White pills on white paper are hard to see (use a coloured tray).
- The camera must stay fixed.

## Hardware
**15. What does the ESP32 do?**
It drives four compartment LEDs (GPIO16, 17, 18, 19 through 220–330 Ω resistors), a buzzer (GPIO25) and a
status LED (GPIO2). It receives short commands over USB serial at 115200 baud:
- `A`–`D`: light that compartment and beep.
- `STOP`: clear.
- `ALARM`: siren.
- `WARN`: double beep.

It replies to each command, so the server knows whether the box responded.

**16. Why an ESP32 and not an Arduino Uno?**
It has built-in Wi-Fi and Bluetooth, more GPIO and memory, and runs at 3.3 V, at a similar price. Today
it is on USB; the planned next step is Wi-Fi firmware. The server already has an HTTP device mode, so the
box could sit at the bedside without a cable to the laptop.

**17. Why not run the camera on an ESP32-CAM?**
Its image quality and processing power are too low for reliable area counting. The laptop does the vision; the ESP32 does
real-time output. Splitting the work this way keeps each part simple.

**18. What if the ESP32 disconnects?**
- The server detects that the box did not reply and flags DEVICE_ERROR.
- Reminders continue on the patient screen and the caretaker app.
- The serial port is found automatically by its USB chip, even if the COM number changes.

## Software
**19. What is the architecture?**
- A Flask server: schedule, dose manager, alert log, and the three web apps.
- A separate camera service process that reports counts to the server.
- A device adapter (serial, Wi-Fi or mock) for the ESP32.

All of it is controlled with one command-line tool, `mv`.

**20. How is a dose decided?**
1. When a dose is due, a 30-minute window opens, with reminders every 5 minutes.
2. The camera reports count changes.
3. The dose manager compares them with the expected compartment and number. The outcomes are:
   - TAKEN
   - TOO_MANY (critical)
   - PARTIAL
   - WRONG_COMPARTMENT
   - MULTI_COMPARTMENT (two or more boxes within 20 s)
   - PUT_BACK
   - UNSCHEDULED_REMOVAL
   - MISSED
   - LOW_STOCK (two or fewer pills left)

**21. How is the caretaker app secured?**
- A password-hash login.
- The first password can only be created on the home laptop.
- Repeated wrong passwords trigger a lock-out.

**22. How did you test it?**
- 261 automated tests: synthetic images, real photos of our sheet (tilted, partly visible, scattered pieces) and dose-logic scenarios.
- A guided hardware run.

**23. What went wrong during development?**
- Crooked hand-drawn lines: solved with angled line detection.
- Pieces touching lines were rejected: solved by using the exact outline instead of a rectangle.
- A false "put back": solved with a stricter rule for counts going up.
- The demo once froze because the camera moved after calibration: we added the "camera moved" alert.

## Future
**24. What would you do next?**
- Wi-Fi firmware.
- Phone push notifications (ntfy is already integrated, but off).
- A 3D-printed box with a fixed camera mount.
- Per-pill colour checks, so a wrong medicine put in a box is noticed.
- A pill detector for real tablets.

**25. Cost?**
About ₹1,500–2,000 for the ESP32, LEDs, buzzer and a USB webcam. The laptop could be replaced by a Raspberry Pi.
