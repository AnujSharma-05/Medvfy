# MedVerify — Hardware Connection Guide

**Purpose:** A reusable wiring and bring-up reference for rebuilding the MedVerify electronics after the kit has been dismantled.

**Current hardware scope:** ESP32 NodeMCU / ESP-32S development board, breadboard, four indicator LEDs, four 220–330 Ω resistors, jumper wires, and the small three-pin buzzer module shown in the project photos.

**Current design deliberately does not use a servo motor or an HC-SR04 ultrasonic sensor.** Do not reconnect those parts for the current firmware.

> **Safety first:** Disconnect the ESP32 from USB before changing any wiring. Never connect 5 V to an ESP32 GPIO. Each LED must have its own series resistor. Read the labels on the physical buzzer before wiring it; the pictured module has a signal pin and a ground pin, with the middle pin unused.

---

## 1. System overview

MedVerify's four LEDs represent the four medicine compartments:

- **A** — GPIO16
- **B** — GPIO17
- **C** — GPIO18
- **D** — GPIO19
- **Buzzer signal** — GPIO25
- **Common ground** — any ESP32 pin marked `GND`

The laptop webcam runs the visual inspection software. The ESP32 runs the separate indicator-and-buzzer firmware. In the current firmware version, reminders are tested through the **USB Serial Monitor**; Wi-Fi/HTTP control of the ESP32 has not yet been implemented in this sketch.

## 2. Parts checklist

- ESP32 NodeMCU / ESP-32S development board and USB data cable
- Breadboard
- Four LEDs, preferably one clearly distinguishable colour per compartment or the colours already used in the project
- Four **220–330 Ω** resistors (one per LED)
- Jumper wires
- The three-pin piezo buzzer module marked approximately `HW-509 V0.2.0` in the project photo (verify the actual marking)
- Laptop with Arduino IDE and the MedVerify controller sketch

Do not substitute a different buzzer module's wiring without checking its printed pin labels and specification.

## 3. Pin map — keep this table

| MedVerify function | ESP32 GPIO | Connects to | Why |
|---|---:|---|---|
| Compartment A LED | GPIO16 | LED A through a 220–330 Ω resistor | Selects/indicates compartment A |
| Compartment B LED | GPIO17 | LED B through a 220–330 Ω resistor | Selects/indicates compartment B |
| Compartment C LED | GPIO18 | LED C through a 220–330 Ω resistor | Selects/indicates compartment C |
| Compartment D LED | GPIO19 | LED D through a 220–330 Ω resistor | Selects/indicates compartment D |
| Buzzer control | GPIO25 | Buzzer pin marked `S` | Firmware generates a short tone on this pin |
| Ground return | GND | LED cathodes and buzzer `−` pin | All components need a shared electrical reference |

**Important:** Use the GPIO number printed on the board, such as `P16`, `P17`, `P18`, `P19`, or `P25`. Do not count header pins from the end of the board; physical header position is not the GPIO number.

## 4. Wiring the four LEDs

Wire **each LED as its own series circuit**. Repeat the same pattern four times, changing only the GPIO number and LED.

```text
ESP32 GPIO16 ── 220–330 Ω resistor ── LED A anode (+, long leg)
                                             LED A cathode (−, short leg / flat edge)
                                                          │
                                                          └── GND
```

Repeat for the other compartments:

```text
GPIO16 ── resistor ── LED A ── GND
GPIO17 ── resistor ── LED B ── GND
GPIO18 ── resistor ── LED C ── GND
GPIO19 ── resistor ── LED D ── GND
```

### How to identify LED polarity

- **Longer leg:** usually the anode (`+`). Connect it toward the GPIO through the resistor.
- **Shorter leg:** usually the cathode (`−`). Connect it to GND.
- **Flat edge on the LED body:** usually marks the cathode side.

If a previously trimmed LED has legs of the same length, use the flat edge or internal electrode shape if visible. Do not guess if polarity is unclear.

### Resistor placement

The resistor can be placed on either side of the LED, as long as it is in series in that LED's circuit. For consistency, use `GPIO → resistor → LED → GND` for all four channels.

**Never connect an LED directly between a GPIO and GND.** The resistor limits current and protects the LED and ESP32 output. Do not share one resistor across multiple independently controlled LEDs.

### Breadboard reminders

- On a typical solderless breadboard, groups of five holes in one row are connected internally; the centre trench separates the two sides.
- Power rails along the edges may be split into separate sections. Do not assume the whole rail is continuous; check it or add a jumper across the break if needed.
- LED legs must not accidentally occupy the same connected breadboard row, or the circuit may be shorted.
- A single GND rail can be used for all four LED cathodes, but connect that rail to an ESP32 `GND` pin.

## 5. Wiring the pictured buzzer module

The project photo shows a small three-pin buzzer board with a `−` marking near one outer pin and an `S` marking near the other. Viewed with the printed text upright and the pins pointing downward, the pictured orientation appears to be:

```text
          Buzzer opening
       ┌─────────────────┐
       │  HW-509 V0.2.0  │  ← verify the exact printed model
       └─────────────────┘
          │     │     │
         (−)   (NC)   (S)
          │             │
         GND          GPIO25
```

Connect it like this **if your actual module has the same `−` and `S` labels**:

| Buzzer pin | Connect to | Notes |
|---|---|---|
| `S` / signal | ESP32 GPIO25 | Firmware generates a tone signal here |
| `−` / GND | ESP32 GND | Common ground |
| Middle pin | Leave disconnected if it is unlabelled/NC as pictured | Do not assume it is VCC |

**Do not add a 3.3 V or 5 V wire to the middle pin unless your exact module is clearly labelled for that connection and its documentation confirms it.** The pictured passive-buzzer style normally uses `S` and `−`; its middle pin is not connected. This is an important point because the three-pin layout can look as if it needs power, even when it does not.

The firmware uses a generated tone rather than merely holding the signal permanently HIGH. That is appropriate for a passive piezo buzzer. If your module's pin markings differ from the diagram, stop and verify the actual model instead of copying this pin orientation blindly.

## 6. Grounding and power

- Power the ESP32 from its USB port for the initial tests.
- Connect all LED cathodes and the buzzer's `−` pin to ESP32 `GND`.
- Do not connect ESP32 GPIO pins to the 5 V rail.
- Do not connect the breadboard's 5 V rail to GPIO16, GPIO17, GPIO18, GPIO19, or GPIO25.
- Avoid adding an external 5 V supply during initial bring-up; the four LED outputs and pictured buzzer can be tested with the simple USB-powered circuit above.
- If you later use a buzzer or other load that needs more current than a GPIO can safely provide, use an appropriate transistor/driver circuit instead of powering the load directly from the GPIO.

Why shared ground matters: a GPIO output is measured relative to the ESP32's ground. Without a shared ground path, the LED or buzzer signal may not have a reliable return path and behaviour can be inconsistent.

## 7. Recommended physical assembly order

Follow this order every time you rebuild the breadboard.

1. **Unplug USB.** This avoids accidental shorts while placing wires.
2. **Seat the ESP32 board.** Make sure the two header rows do not short against each other or against exposed metal.
3. **Connect GND.** Connect an ESP32 `GND` pin to the breadboard's chosen ground row/rail.
4. **Wire LED A only.** GPIO16 → resistor → LED anode; LED cathode → GND.
5. **Add LEDs B, C, and D.** Use GPIO17, GPIO18, and GPIO19, each with its own resistor.
6. **Inspect the circuit.** Look for a resistor in every LED path, correct LED orientation, and no jumper bridging GPIO directly to GND or a power rail.
7. **Wire the buzzer.** On the pictured board, connect `S` → GPIO25 and `−` → GND; leave the middle pin unconnected if it is NC.
8. **Inspect the pin labels one more time.** Pay special attention to GPIO25 and buzzer polarity.
9. **Reconnect USB only after the inspection.** If the board resets repeatedly, a component gets hot, or the buzzer behaves unexpectedly, disconnect USB immediately and inspect the wiring.

## 8. Upload and test the controller firmware

### Arduino IDE settings

1. Open the MedVerify controller sketch: `MedVerify_Controller.ino`.
2. Select **Tools → Board → esp32 → ESP32 Dev Module** (the exact menu wording can differ by IDE/package version).
3. Select the COM port that Windows assigned to the ESP32. The previously observed `COM28` may change; check **Device Manager → Ports (COM & LPT)** rather than assuming the port number is permanent.
4. Upload the sketch.
5. Open **Tools → Serial Monitor**.
6. Set the baud rate to **115200** and line ending to **Newline**.
7. The boot text should include `MedVerify controller booted.` and the available command list.

### Serial commands

Enter each command into the Serial Monitor and press Enter.

| Command | Expected behaviour |
|---|---|
| `HELP` | Prints the command list |
| `TEST` | Tests LEDs A–D in sequence and plays short buzzer tones |
| `A` | Turns LED A on and sounds the buzzer |
| `B` | Turns LED B on and sounds the buzzer |
| `C` | Turns LED C on and sounds the buzzer |
| `D` | Turns LED D on and sounds the buzzer |
| `STOP` | Turns all four LEDs and the buzzer off |

The selected compartment LED remains on until `STOP` is entered or another compartment is selected. This is expected behaviour in the current sketch.

### What a successful test looks like

- `TEST` visibly lights the four compartment LEDs one at a time.
- The buzzer produces short beeps during the test.
- `A`, `B`, `C`, and `D` each select the corresponding LED.
- `STOP` switches all four LEDs off and stops the buzzer.
- The Serial Monitor prints the corresponding status messages.

## 9. Troubleshooting checklist

### An LED does not light

1. Check that its command matches the correct GPIO.
2. Check the LED polarity.
3. Verify that its resistor is in series and both leads are in the intended breadboard rows.
4. Confirm the LED cathode and ESP32 `GND` share the same ground connection.
5. Check that the firmware's GPIO mapping still matches this guide.

### The wrong LED lights

Check the mapping before moving wires. The firmware map is A=16, B=17, C=18, D=19. Do not swap labels based on breadboard position alone.

### The buzzer does not sound

1. Confirm the physical board's `S` pin goes to GPIO25.
2. Confirm its `−` pin goes to ESP32 GND.
3. Confirm the middle pin remains disconnected if it is marked/known as NC.
4. Confirm the Serial Monitor sends `TEST` or a compartment command with **Newline** selected.
5. Confirm the uploaded firmware uses GPIO25 and a tone/PWM-generating function.
6. If the marking or pinout differs, disconnect power and identify the exact module first.

### The buzzer clicks, buzzes continuously, or makes an unexpected noise

Disconnect USB immediately. Do not keep moving wires while powered. Recheck the `S` and `−` pins and confirm that the signal pin has not been connected to a supply rail. A passive buzzer requires a changing signal to produce a tone; a continuous supply or an incorrect connection may not behave as expected.

### Upload fails or the port disappears

1. Try a known data-capable USB cable.
2. Recheck **Device Manager** for the USB-serial adapter and current COM number.
3. Close any other application that may have opened the same serial port.
4. Re-select `ESP32 Dev Module` and the current COM port.

## 10. Common wiring mistakes to avoid

- Forgetting the resistor on one LED.
- Putting all LED cathodes on a breadboard rail that is not connected to ESP32 GND.
- Using the GPIO's physical header position as though it were the GPIO number.
- Connecting 5 V to an ESP32 GPIO.
- Treating the pictured buzzer's middle pin as VCC without checking the silkscreen.
- Reversing `S` and `−` on the buzzer.
- Rewiring while the ESP32 is connected to USB.
- Reintroducing the removed servo or HC-SR04 into the current setup.
- Assuming the ESP32 already receives Wi-Fi/HTTP reminder commands. This sketch currently accepts USB Serial commands only.

## 11. Before dismantling the lab setup

Before unplugging anything, take a clear photo of the whole circuit and one close-up of the ESP32 labels and buzzer labels. Then:

- Enter `STOP` in the Serial Monitor.
- Close the Serial Monitor.
- Unplug the USB cable.
- Remove the jumper wires and components carefully.
- Keep LEDs and their corresponding resistors together, if possible.
- Record anything that differed from this guide so the guide can be corrected rather than relying on memory next time.

## 12. Quick rebuild card

```text
LED A: GPIO16 → 220–330 Ω → LED anode; LED cathode → GND
LED B: GPIO17 → 220–330 Ω → LED anode; LED cathode → GND
LED C: GPIO18 → 220–330 Ω → LED anode; LED cathode → GND
LED D: GPIO19 → 220–330 Ω → LED anode; LED cathode → GND
Buzzer: S → GPIO25; − → GND; middle pin NC/unconnected if marked as pictured
Power: ESP32 USB during testing; all components share ESP32 GND
Serial Monitor: 115200 baud, Newline
Commands: TEST, A, B, C, D, STOP, HELP
Not used in current version: servo, HC-SR04
```

## 13. Technical references

- [Espressif ESP32 datasheet](https://documentation.espressif.com/esp32_datasheet_en.html) — pin functions and electrical characteristics.
- [Passive piezo buzzer module guide](https://www.electrokit.com/upload/quick/f4/bd/b429_Userguide.pdf) — typical three-pin passive buzzer wiring (`S`, unused middle pin, `−`).
- [HW-508 passive buzzer module reference](https://circuit-electronics.com/product/hw-508-passive-buzzer-module/) — typical signal and ground pin arrangement.

**Last reviewed:** October 2026. Re-check the silkscreen on the physical board and the GPIO constants in `MedVerify_Controller.ino` if the hardware or firmware changes.
