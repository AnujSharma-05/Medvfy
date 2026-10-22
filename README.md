# MedVerify — Vision-Assisted Medication State Verification

MedVerify monitors designated medication compartments (A, B, C, D) using computer vision to track visible state transitions (medication present, removed, returned, partial/unexpected, or uncertain) to assist elderly users with medication adherence.

> [!CRITICAL]
> **Safety Boundary & Non-Ingestion Guarantee**
> Visual verification of compartment emptiness is **NOT proof of ingestion**. MedVerify reports visible physical compartment state changes only. It **never** labels a dose as ingested merely because a compartment became empty. Ambiguous observations or degraded camera frames explicitly report `UNCERTAIN`.

**Start here:**
- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) explains the full flow (schedule → reminder → camera → outcome → notifications), known flaws, notification setup, the laptop test, and trial mode for tuning the camera.
- [docs/flow-map.html](docs/flow-map.html) (open in a browser) has the end-to-end user and technical flow diagrams: caretaker, patient, server and camera, plus the design decisions behind the apps.
- [docs/AGENT_CONTEXT.md](docs/AGENT_CONTEXT.md) holds the project's ground rules for engineers and AI agents (safety boundaries, invariants, milestones).

```powershell
.\run_medverify.bat     # server + console (http://127.0.0.1:5000) + camera window
.\run_fake_camera.bat   # drive the server from the keyboard, no webcam needed
.\run_tests.bat         # full offline test suite
```

Once the server runs: patient screen `http://127.0.0.1:5000/patient`, caretaker app
`http://127.0.0.1:5050/care` (create its password first at `http://127.0.0.1:5000/care` on the laptop),
developer console `http://127.0.0.1:5000/console`. See [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) §11.

---

## 1. Architecture & Modules

```
Medvfy/
├── backend/
│   ├── app.py                # Flask API + scheduler thread (/api/status, /api/schedule, /api/reminders, /api/observations, /api/vision/heartbeat, /api/notifications)
│   ├── dose_manager.py       # Dose sessions: start, match camera events, escalate, REMOVED/MISSED/PARTIAL
│   ├── schedule.py           # Daily dose times per compartment (config/schedule.json)
│   ├── notifier.py           # In-app feed + optional phone push (ntfy)
│   ├── device_adapter.py     # MockDevice, ESP32SerialDevice (existing firmware), ESP32HTTPDevice (future)
│   ├── auth.py               # Caretaker password (hashed), login lockout
│   ├── commands.py           # Server → camera commands (recalibrate) via heartbeat replies
│   └── static/               # care.html (son), patient.html (father), console.html (dev), index.html
├── tools/fake_camera.py      # Keyboard-driven fake camera for testing without a webcam
├── docs/HOW_IT_WORKS.md      # End-to-end explanation, flaws, setup, test protocol
├── config/
│   ├── settings.json         # Camera resolution, active index, compartments, and backend URL
│   └── rois.json             # Compartment region-of-interest coordinates & camera resolution metadata
├── data/
│   ├── vision_baseline.npz   # Immutable expected & empty reference crops
│   ├── vision_baseline.json  # Reference store metadata & calibration timestamps
│   ├── vision_observations.csv # Local durable CSV log of all visual transitions
│   └── observations.jsonl    # Backend observation append log
├── vision/
│   ├── camera_inspector.py   # Webcam window: calibration keys, continuous monitoring, heartbeat
│   ├── monitor.py            # ContinuousMonitor: stable-scene gate + periodic evaluation
│   ├── reference_store.py    # ReferenceStore: isolated expected/empty baselines; atomic saves
│   ├── state_machine.py      # CompartmentStateMachine: debouncing, duplicate suppression, state enum
│   ├── frame_quality.py      # FrameQualityAssessor: blur (Laplacian), exposure, motion stability
│   ├── roi_manager.py        # ROIManager: bounds validation, resolution matching, atomic persistence
│   ├── comparator.py         # VisualComparator: CLAHE illumination norm, SSIM, color correlation, blobs
│   ├── event_logger.py       # EventLogger: durable CSV logging + resilient backend POST
│   └── classifier_interface.py# BaseMedicationClassifier & ClassicalHeuristicClassifier
└── tests/                    # Automated offline unit, scenario, and integration tests
```

---

## 2. Compartment States & Semantics

| State | Semantic Meaning |
| :--- | :--- |
| `UNKNOWN` | Compartment has not yet been calibrated or evaluated. |
| `EXPECTED_PRESENT` | Visible contents match the calibrated expected medication baseline. |
| `REMOVAL_CANDIDATE` | Transient observation of removal (debouncing in progress). |
| `REMOVAL_DETECTED` | Confirmed physical removal of expected medication from compartment. |
| `RETURN_CANDIDATE` | Transient observation of medication return (debouncing in progress). |
| `OBJECT_RETURNED` | Confirmed return of medication matching expected baseline configuration. |
| `PARTIAL_OR_WRONG` | Compartment contains objects inconsistent with expected baseline (wrong token, partial dose). |
| `EMPTY` | Confirmed empty compartment matching calibrated empty reference. |
| `UNCERTAIN` | Camera blur, motion, severe exposure, hand occlusion, or ambiguous metrics. |
| `CAMERA_ERROR` | Camera disconnected, frame read failure, or incompatible region coordinates. |

---

## 3. Setup & Execution (Windows PowerShell)

Run within the workspace virtual environment:

```powershell
# 1. Activate virtual environment
.\.venv\Scripts\Activate.ps1

# 2. Run automated test suite (completely offline)
python -m unittest discover -s tests -v

# 3. Start local development server (Terminal 1)
python -m backend.app
# Access console at http://127.0.0.1:5000

# 4. Start live camera inspection (Terminal 2)
python vision/camera_inspector.py
```

---

## 4. Camera Inspector Controls

Click inside the OpenCV camera window to send key commands:

- **`R` (Define Regions)**: Sequentially select bounding boxes for compartments A, B, C, D (drag box, press `ENTER`/`SPACE` to confirm, `C` to cancel).
- **`S` (Save Expected Baseline)**: Arrange scheduled medications in compartments and press `S` to establish the immutable expected reference baseline.
- **`E` (Save Empty Baseline)**: Clear compartments and press `E` to record empty compartment references.
- **`M` (Monitor)**: Start/stop continuous monitoring. Evaluates every 0.5 s once the scene has been still for 1 s; a removal/return is confirmed only after it persists ≥ 2 s. Starts automatically on launch when calibration exists (`monitor_on_start`).
- **`V` (Compare Once)**: Evaluates the current frame immediately, bypassing the stillness gate. Visual comparisons **never overwrite** the expected reference baseline.
- **`C` (Switch Camera)**: Cycles through connected video capture devices (0–4).
- **`Q` (Quit)**: Safely releases camera hardware and closes windows.

---

## 5. Verification Protocol

Follow [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) §9: Part A exercises the server,
dose sessions and notifications with the fake camera; Part B repeats it with the real
camera and dummy objects. Calibrate in this order: `R` → empty box → `E` → load box → `S` → `M`.

---

## 6. Known Limitations & Next Steps

- **Chemical Pill Identification**: Classical computer vision methods (color histograms, contour morphology) verify shape, color family, and item counts, but cannot certify chemical formulation or pharmaceutical identity.
- **Overlapping Pills**: Touching or clustered pills require instance segmentation or depth sensing for individual pill counting.
- **Hardware Integration**: The backend runs in `mock` device mode by default. Set `device_mode: "serial"` and `serial_port` (e.g. `COM5`) to drive the existing ESP32 firmware over USB. See [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) §5 for the full list of known flaws.
