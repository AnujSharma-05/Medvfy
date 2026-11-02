# MedVerify: viva and presentation preparation

How to use this: learn Part 1 almost word for word. Read Parts 2–5 twice; the answers are short on
purpose, so you can say them in one breath. Detailed technical answers are in
[EVALUATOR_QA.md](EVALUATOR_QA.md), and the demo steps (which command, which hand) are in
[DEMO_SCRIPT.md](DEMO_SCRIPT.md).

**Golden rules**
1. Answer in one or two sentences, then stop. If they want more, they will ask.
2. Do not overclaim. Say "the camera confirms removal from the box, not swallowing". Evaluators trust a team that knows its limits.
3. If you don't know: *"We haven't measured that. Here is how we would find out: ..."*. Never invent a number.
4. Turn weaknesses into design decisions: *"We chose X because Y; the trade-off is Z."*

---

## Part 1: the presentation script (about 7 minutes)

**P1 = speaker** (explains). **P2 = operator** (types the commands, handles the box).

### 1. Opening (P1, 30 s)
> "Good morning, sir. We are Anuj Sharma and Ishaan Goswami, and our project is **MedVerify**, an
> IoT pill box that does not just remind elderly patients to take their medicine; it **checks** that
> they took the right number of pills from the right compartment."

### 2. The problem (P1, 45 s)
> "About half of patients with long-term illness do not take their medicines as prescribed, according
> to the WHO. For the elderly the common mistakes are forgetting, taking a double dose and taking from
> the wrong strip. Their children often live in other cities and cannot check every day. Existing smart
> boxes only detect that a lid was opened. They cannot tell one pill from three, or box A from box B."

### 3. Our solution (P1, 45 s, pointing at each part)
> "Our system has three parts:
> - Four compartments, A to D, drawn on a sheet.
> - An **ESP32** with one LED per compartment and a buzzer.
> - A **webcam** above that **counts the pills in every compartment** using OpenCV.
>
> A Flask server on the laptop ties it together and serves three apps: an **admin console** for
> set-up, a **caretaker app** for the son or daughter, and a **patient screen** in English and Hindi."

### 4. Live demo (P2 acts, P1 narrates, 4 min)
Follow [DEMO_SCRIPT.md](DEMO_SCRIPT.md) sections 3–7. Lines for P1 while P2 acts:

- **Correct dose:**
  > "The caretaker's schedule opened a dose for box A: light on, beep, and the screen says *take
  > one from A*. He takes one... the hand leaves... about two seconds later the camera confirms the
  > count went from 4 to 3. The light goes off, the screen says Done, and the caretaker sees *taken*."
- **Too many:**
  > "Now he takes two instead of one. Siren, red *Stop* screen, and a critical alert to the
  > caretaker, because a double dose of a blood-pressure tablet can be dangerous."
- **Wrong box:**
  > "Here the dose is for C but he takes from D. The screen names the right box, and C's light beeps
  > again."
- **Hand guard:**
  > "A hand resting over a box is never counted as a removal: anything lying across the pen
  > line blocks that box's reading until it moves away."

### 5. How the counting works (P1, 45 s)
> "At set-up we take one photo of the empty sheet and one with known counts. Whatever differs from
> the empty photo is medicine. Its **area divided by the area of one pill** is the count. Area stays
> the same when pills are moved or touch each other, which simple blob counting cannot handle. A new
> count must hold steady for two readings over one second before we act on it, so a moving hand never
> triggers a false alarm."

### 6. Close (P1, 30 s)
> "To summarise: reminders on the box, verification by the camera, instant correction for the
> patient and peace of mind for the family. Next, we would move the ESP32 to Wi-Fi so the box
> needs no cable, add phone push notifications and build a 3D-printed box with a fixed camera mount.
> Thank you. We are happy to take questions."

---

## Part 2: follow-up questions right after the demo

| Question | Short answer |
|---|---|
| How long does detection take? | About 2 seconds after the hand leaves: 1 s to make sure the scene is still, 1 s to confirm the count. |
| What if he takes the pill and puts it back? | The count goes back up, so it is recorded as *put back*, not as a dose taken. |
| What if he never comes? | Reminders repeat every 5 minutes. After 30 minutes the dose is marked MISSED and the caretaker is alerted. |
| What if the camera or sheet is moved? | All boxes look blocked. After 10 s the caretaker gets a critical *camera moved* alert. Re-set-up takes 2 minutes. |
| What if the light changes? | Brightness and colour are normalised against the paper, and too dark or blurred frames are rejected. |
| Why did it take a second longer that time? | It waits until the scene is completely still, by design. We prefer slow and right to fast and wrong. |
| What if the internet is down? | Everything runs locally on the laptop; no internet is needed. Only remote phone notifications would need it. |
| What if the ESP32 is unplugged? | The server notices that the box stopped replying and flags a device error. Reminders continue on the screen and the caretaker app. |
| Can it tell which medicine it is? | It knows the compartment. Each compartment holds one medicine, set by the caretaker. Recognising a wrong tablet placed in a box is future work. |
| Why paper pieces, not real pills? | For a safe, repeatable demo. Real flat tablets are easier to count, because crumpled paper changes its visible area. |
| Is it accurate? | On our sheet the estimates sit within about ±0.1 of the true count when the pieces are still. Ambiguous readings are not acted on, and decreases between two counts are decided only after 3 s steady. |
| Does the patient need to do anything special? | No. Take the pills normally. The only rule is one compartment at a time, and the system reminds him of that too. |

---

## Part 3: questions about the design ("why did you...?")

| Question | Short answer |
|---|---|
| Why a camera, and not a weight sensor or lid switch? | A lid switch only knows the box was opened. A load cell per compartment would work, but costs a sensor per box and drifts. One camera watches all compartments and can also say *which* box and *how many*. |
| Why ESP32 and not Arduino Uno? | Built-in Wi-Fi and Bluetooth, a 240 MHz dual core, more memory and GPIO, at a similar price. It lets us move to wireless without changing the board. |
| Why is the ESP32 on USB if it has Wi-Fi? | USB serial is the most reliable link for a live demo and also powers the board. The server already has a Wi-Fi (HTTP) device mode; the Wi-Fi firmware is the next step. |
| Why not an ESP32-CAM? | Its image quality and processing are too low for reliable counting. The laptop does the vision; the ESP32 does real-time output. |
| Why classical OpenCV and not deep learning (YOLO)? | No labelled dataset, no GPU needed, results in milliseconds, and every decision is explainable. A trained detector is a natural upgrade for real pill shapes. |
| Why count by area, not by counting blobs? | Pills touching each other merge into one blob, so blob counting fails. Area adds up correctly whether the pills touch or not. |
| Why pen-drawn squares? | Low cost and flexible for a prototype: the software finds the squares automatically, even tilted up to 15°. A real product would use a moulded tray. |
| Why Flask? | Lightweight Python web framework, the same language as our OpenCV code. One server can host the API and all three apps. |
| Why three separate apps? | Three different users with different needs. The patient needs one big instruction, the caretaker needs history and alerts, the admin needs set-up and health. |
| Why English and Hindi? | Many elderly users in India are more comfortable in Hindi. Large bilingual text reduces mistakes. |
| Why a separate camera process? | If the camera hangs, the server and reminders keep running. The two processes talk over HTTP. |
| How do the parts communicate? | Camera service → server: HTTP (JSON). Server → ESP32: USB serial, short text commands with a reply. Apps → server: HTTP from the browser. |
| How is the caretaker app secured? | Salted password hash (Werkzeug), the first password can only be created on the home laptop, and a 5-minute lock-out after 5 wrong attempts. |
| How did you test it? | 265 automated tests: synthetic images, real photos of our sheet (tilted, partly visible, scattered pieces) and the dose-logic scenarios. Then live runs with the real camera and box. |

---

## Part 4: general IoT and technical questions (beyond the project)

### IoT basics
| Question | Short answer |
|---|---|
| What is IoT? | Physical devices with sensors and actuators, connected over a network, that collect data and act on it with little human effort. |
| Layers of IoT architecture? | Three-layer model: **perception** (sensors, actuators), **network** (transport) and **application** (services, apps). Ours: camera + LEDs/buzzer, USB/HTTP, Flask apps. |
| What are sensors and actuators here? | Sensor: the webcam. Actuators: the LEDs and buzzer. |
| Edge vs cloud computing? | Edge processes data near the device (fast, private, works offline); cloud processes it on remote servers (scalable, accessible anywhere). We do edge processing on the laptop. |
| Why edge here? | Medical images stay at home (privacy), there is no internet dependency, and the response takes seconds. |
| What is a microcontroller vs a microprocessor? | A microcontroller has CPU, memory and I/O on one chip for dedicated control (ESP32). A microprocessor is only the CPU and needs external memory; it runs an OS (laptop, Raspberry Pi). |

### Communication protocols
| Question | Short answer |
|---|---|
| What is MQTT? | A lightweight publish/subscribe protocol over TCP. Devices publish to topics on a broker and subscribers receive them. QoS levels 0, 1 and 2. Default port 1883, or 8883 with TLS. |
| HTTP vs MQTT? | HTTP is request/response and heavier per message; MQTT is publish/subscribe, very light and keeps a connection open. MQTT suits many sensors sending often; HTTP is simple for occasional commands like ours. |
| What is CoAP? | A lightweight REST-like protocol over UDP for very constrained devices. |
| What is UART / serial? | Asynchronous two-wire (TX/RX) communication with an agreed baud rate. We use 115200 baud over the USB-to-UART chip (CP210x). |
| What is baud rate? | Symbols per second on the serial line; here 115200 bits per second. |
| I2C vs SPI vs UART? | I2C: 2 wires, many devices by address, slower. SPI: 4 wires, fast, a chip-select per device. UART: 2 wires, point-to-point, no clock line. |
| Wi-Fi vs Bluetooth vs Zigbee vs LoRa? | Wi-Fi: high data rate, more power. BLE: short range, low power. Zigbee: low-power mesh. LoRa: kilometres of range at very low data rates. |
| What is a REST API? | A web interface where resources have URLs and are read or changed with HTTP methods (GET, POST...), usually in JSON. Our server exposes one, e.g. `/api/status`. |

### ESP32 and electronics
| Question | Short answer |
|---|---|
| ESP32 specs? | Dual-core Xtensa LX6 up to 240 MHz, about 520 KB SRAM, Wi-Fi 802.11 b/g/n (2.4 GHz), Bluetooth 4.2 + BLE, 3.3 V logic, many GPIO with ADC, PWM, I2C, SPI and UART. |
| Why a resistor with each LED? | To limit the current. Without it the LED and the GPIO pin can be damaged. 220–330 Ω at 3.3 V gives a safe few milliamps. |
| Active vs passive buzzer? | An active buzzer has its own oscillator and beeps on DC; a passive one needs a frequency signal (PWM/tone). We generate patterns from the ESP32 for the siren and warning. |
| What is GPIO? | General-purpose input/output: a pin the program can read or drive high and low. |
| What is PWM? | Pulse-width modulation: switching a pin rapidly, with the duty cycle setting the average power. Used for dimming and tones. |
| Is 3.3 V vs 5 V an issue? | ESP32 pins are 3.3 V and not 5 V tolerant; we power the LEDs and buzzer from 3.3 V, so no level shifting is needed. |
| How is the firmware uploaded? | Compiled and flashed over USB with arduino-cli, from VS Code (`mv firmware upload`). |

### Computer vision
| Question | Short answer |
|---|---|
| What is OpenCV? | An open-source computer-vision library with image processing, feature detection and camera access. We use it from Python. |
| Why the Lab colour space? | It separates lightness (L) from colour (a, b), so a shadow changes L but not colour. That helps us tell medicine from shadow. |
| What is morphology (opening/closing)? | Opening = erode then dilate, which removes small specks. Closing = dilate then erode, which fills small gaps. We use them to clean masks and bridge gaps in pen lines. |
| How do you detect blur? | The variance of the Laplacian: sharp images have strong edges and a high variance. Frames below half the calibrated sharpness are rejected. |
| How do you detect motion? | Compare consecutive frames (after a slight blur, to ignore pixel noise) and check what fraction of pixels changed. Above 2% counts as moving. |
| What is thresholding? | Turning an image into black and white by a cut-off value, to separate an object from the background. |
| What is a region of interest (ROI)? | The part of the image we analyse; here each drawn square. |

### Software, data and security
| Question | Short answer |
|---|---|
| What is a state machine? Where do you use it? | A model with defined states and transitions. Each compartment moves through states such as present, removal candidate, removal confirmed and uncertain, which stops one noisy frame from triggering an event. |
| What is debouncing? | Requiring a change to persist before accepting it. We need 2 readings over 1 s. It is the same idea as debouncing a push button. |
| Where is data stored? | Local files: JSON and JSONL for dose history and events, CSV for camera observations, and NPZ (NumPy) for the calibration photos. |
| Is the patient's data private? | Images never leave the laptop. Only text events (taken, missed, alerts) would be sent to the caretaker. |
| How would you secure a cloud version? | HTTPS/TLS, authenticated MQTT, per-user accounts, encryption at rest and minimum data: send counts, not images. |
| What is hashing vs encryption? | A hash is one-way (passwords); encryption is reversible with a key (data in transit or at rest). |

### Product and ethics
| Question | Short answer |
|---|---|
| Cost? | About ₹1,500–2,000 for the ESP32, LEDs, buzzer and webcam. The laptop could be replaced by a Raspberry Pi (about ₹5,000). |
| How would it scale to many homes? | Each home stays an edge unit. Events go over MQTT/HTTPS to a cloud dashboard where one caretaker or clinic can follow many patients. |
| Power failure? | The ESP32 and laptop need power. A product would add a battery backup, and the caretaker would be alerted when the box goes offline. |
| Is camera monitoring ethical? | The camera looks only at the pill sheet, not at the person, and images stay local. Consent of the patient is part of set-up. |
| Is it a medical device? | No. It is an adherence aid. A certified product would need clinical validation and regulatory approval. |
| What is novel here? | Counting pills per compartment with one camera, and judging the dose (right count, right box, too many) in real time, at a very low cost. |
| What was the hardest problem? | Reliability in real conditions. Shiny crumpled pieces glint, which looked like motion and blocked confirmation; a moved camera froze the readings. We found both from recorded live data and fixed them, with tests. |
| What did you learn? | Testing with real hardware finds problems that simulations do not. Also: log everything, so a failure can be explained instead of guessed. |
| Individual contributions? | Say concretely who did what: for example, one of you led the vision and counting, the other the ESP32 firmware, server and apps, and you shared testing and the report. Agree on this before the viva. |

---

## Part 5: tough or critical questions, and calm answers

| Question | Short answer |
|---|---|
| "This is just a webcam on a laptop. Where is the IoT?" | The ESP32 is a networked actuator node, the camera is the sensor, and the server connects them to remote users. That is perception, network and application. Wi-Fi firmware makes the box fully wireless. |
| "What if the patient takes the pill and throws it away?" | We can't detect that, and we say so clearly. We confirm removal from the box, which already catches the most common errors: forgetting, double dose and wrong box. |
| "What if two medicines look the same?" | Each compartment holds one medicine, so the box tells us which medicine. Colour checks per pill are future work. |
| "Your accuracy with crumpled paper is not perfect." | True. Crumpled paper changes its visible area by up to 25%. We added tolerance and a steady-reading rule; real flat tablets are more consistent. |
| "Why should anyone buy this instead of a ₹200 pill organiser?" | An organiser cannot remind, verify or alert the family. For a patient on several critical medicines, catching one double dose is worth it. |
| "What happens if your software crashes?" | The camera and server are separate processes; the server keeps reminders going and reports the camera as offline. The ESP32 keeps its last state. |
| "What if the elderly person cannot read?" | The LED shows the box and the buzzer gives the cue. Voice prompts in Hindi would be an easy addition. |
| "Did you write this yourselves?" | Yes. We designed the architecture, wrote and tested the code and firmware, wired the hardware and debugged it live. We used AI coding assistance as a tool, as an engineer uses documentation. (Be honest if asked.) |

---

## Part 6: numbers to remember

| Item | Value |
|---|---|
| Compartments | 4 (A–D) |
| ESP32 pins | LEDs GPIO16/17/18/19, buzzer GPIO25, status LED GPIO2 |
| Serial | 115200 baud, over the CP210x USB chip |
| Camera | 640×480, checked every 0.5 s |
| Detection | Still 1 s, then 2 readings over 1 s: about 2 s in total |
| Doses | Window 30 min, reminders every 5 min |
| Alerts | 2+ boxes within 20 s = one at a time. Camera moved after 10 s blocked. Patient alert stays 90 s or until OK. |
| Low stock | 2 or fewer pills left |
| Login | 5 wrong passwords = 5-minute lock-out |
| Tests | 265 automated |
| Cost | About ₹1,500–2,000 without the laptop |
