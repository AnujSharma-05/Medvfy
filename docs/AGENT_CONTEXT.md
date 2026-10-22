# MedVerify — Project Context for AI Engineering Agents

> **Purpose of this file:** Give any AI coding agent, engineer, or reviewer a shared, first-principles understanding of MedVerify before they inspect or change code.
>
> **How to use it:** Read this file first, then inspect the live repository. This document captures the project direction and the latest reported QA results, but it is not a substitute for reading the current code, running tests, or checking `git status`.

---

## 0. Instructions to the AI Agent Reading This File

You are contributing to an existing software project, not starting a new one from scratch.

Your first responsibility is to understand what the system is meant to prove, what it can currently measure, and what it must never claim. Your second responsibility is to improve the system without losing working functionality or the user's changes.

Follow these rules on every task:

1. **Inspect before editing.** Read the affected files, the callers, the tests, configuration, and the relevant Git diff before changing code.
   - **WHY:** A local-looking change can break a contract elsewhere in the application. Understanding the full path prevents accidental regressions.
2. **Verify the current state.** Run `git status` and the appropriate tests. Treat the repository and actual command output as the source of truth.
   - **WHY:** This project is evolving quickly. A report from an earlier turn can become stale after code changes.
3. **Preserve existing work.** Do not use `git reset`, `git clean`, broad reverts, or destructive file operations. Never overwrite user changes just to make the tree clean. Do not commit or push unless explicitly asked.
   - **WHY:** Uncommitted and untracked files may contain active implementation work, experiments, or prompts that have not yet been committed.
4. **Work in small, complete increments.** Fix a concrete problem, add tests, run them, inspect the diff, then proceed.
   - **WHY:** Small changes are easier to understand, debug, test, and reverse than a broad rewrite.
5. **Test behaviour, not just syntax.** A program that opens a camera window or compiles successfully is not necessarily correct.
   - **WHY:** Many of MedVerify's most important defects are logical: confusing a reference with an observation, duplicating events, or treating uncertain evidence as a confirmed state.
6. **Never invent evidence.** Report exact test counts and measured metrics only when command output or reproducible data supports them. Label synthetic tests separately from physical-camera tests.
   - **WHY:** False confidence is particularly risky in a medication-related product.
7. **Prefer explicit uncertainty to a confident guess.** Missing references, blur, occlusion, poor exposure, or conflicting evidence should produce a safe uncertain result.
   - **WHY:** A wrong confident result is more harmful than acknowledging that the available image is insufficient.
8. **Keep compatibility in view.** Check the Flask API, CSV format, persisted state, configuration format, and camera-inspector controls before changing them.
   - **WHY:** The vision code is one part of a larger system; a better detector is not useful if it breaks logging or the surrounding application.
9. **Explain the WHY.** When changing an algorithm, threshold, state, interface, dependency, or data format, explain the reason and the trade-off.
   - **WHY:** Future maintainers must be able to judge whether a decision remains correct as the project grows.
10. **Do not overclaim medical capability.** Visual disappearance means the object may have been removed from view. It does not prove that a person swallowed it, and appearance alone may not prove chemical identity.
    - **WHY:** The camera observes a compartment, not the person's mouth, swallowing process, or the chemical composition of a tablet.

If a task is ambiguous, proceed with the safest small improvement that is supported by the code and requirements. Ask a question only when an essential decision cannot be inferred safely. Do not spend the entire task producing another plan when implementation and testing are possible.

---

## 1. What MedVerify Is

### 1.1 Product in plain language

MedVerify is a vision-assisted medication compartment monitoring system intended to help a person, especially an older adult, follow a medication routine.

The planned container has separate compartments, currently labelled **A, B, C, and D**. A camera looks at the compartments. The software records what the compartments are expected to look like, observes them later, and reports whether the visible contents appear unchanged, removed, returned, partially changed, inconsistent, or uncertain.

A wider system is intended to support scheduled reminders. At reminder time, the application can tell the user which compartment to access. A future hardware path can activate the relevant LED and buzzer through an ESP32. The current software-development focus is the camera pipeline and backend, and laptop/webcam-only work must remain possible when lab hardware is unavailable.

### 1.2 The central problem

The original prototype asks a simple question:

> “Does this compartment look different from the previous image?”

The product needs to answer a more useful, more difficult question:

> “Does the available visual evidence support the conclusion that the expected visible contents remain present, have been removed, have returned, or differ from the expected configuration?”

Those questions are not equivalent.

A raw image difference can be caused by a shadow, lighting variation, camera movement, focus changes, a hand covering the scene, a pill changing position, a pill being removed, or a new object being introduced. A detector must not treat all those causes as the same event.

**WHY this distinction matters:** A change detector measures visual difference. A state-verification system interprets evidence over time and has to reject cases where the evidence does not support a safe conclusion.

### 1.3 Current intended capability versus future goal

Keep these capability levels distinct:

- **Visible-state monitoring:** Determine whether a compartment's visible appearance has changed. This is the initial foundation.
- **Removal/return tracking:** Use references and temporal state logic to decide whether evidence supports a removal or a return.
- **Quantity estimation:** Estimate how many visible objects remain, and identify partial changes.
- **Medication classification:** Determine whether a visible object resembles a specific, known medication class using suitable training or reference data.
- **Ingestion verification:** Prove that the person swallowed the medication. The current camera/compartment system cannot establish this.

**WHY these are separate levels:** Each level requires different evidence. A system that detects a changed image has not thereby shown that a pill was removed; a system that estimates one object is present has not thereby identified the medication; and an empty compartment does not establish ingestion.

---

## 2. Safety and Truthfulness Rules

These rules apply to the code, UI, logs, tests, API messages, and project documentation.

1. **Never report ingestion as verified from compartment imagery.** Use language such as `REMOVAL_DETECTED`, `VISIBLE_CONTENTS_CHANGED`, or `OBJECT_RETURNED` only when evidence supports it.
2. **Do not claim medication identity based only on colour or approximate shape.** Similar-looking pills may have different identities or strengths.
3. **Do not claim the expected quantity is correct unless the quantity detector has been evaluated against labelled data.**
4. **Use `UNCERTAIN` when the camera frame or comparison is unreliable.** Do not quietly convert invalid evidence to a successful or failed medication state.
5. **Do not interpret a returned object as the expected medicine merely because something appears in the compartment.** Compare against the expected reference and apply the project's matching rules.
6. **Do not claim real-world accuracy from unit tests alone.** Synthetic fixtures can validate code paths, but they cannot reproduce every optical condition, camera behaviour, pill geometry, or real compartment.

**WHY conservative reporting is required:** False reassurance can hide a missed dose or a wrong compartment state. The system should communicate what it observed, what it inferred, and what remains unknown.

---

## 3. First-Principles Model of the System

Think of the system as a chain of evidence. Every stage should have a clear input, output, and reason for existing.

1. **Camera acquisition:** Get an actual frame from the chosen camera.
   - **WHY:** All later decisions depend on the image being real, fresh, and correctly captured.
2. **Frame quality assessment:** Determine whether the image is sharp, stable, properly exposed, and usable.
   - **WHY:** Bad input can make a correct algorithm produce a false result.
3. **Region-of-interest (ROI) management:** Extract the section corresponding to each compartment.
   - **WHY:** Each compartment must be evaluated independently; an event in A must not be confused with B.
4. **Reference storage:** Save separate expected and empty reference images, with metadata.
   - **WHY:** “What should be here?” and “What does empty look like?” are different questions and require different reference states.
5. **Visual comparison:** Calculate evidence about the current image relative to the references.
   - **WHY:** The state machine should consume interpretable metrics rather than implement image-processing details itself.
6. **State machine:** Combine observations over time and decide whether a transition is supported.
   - **WHY:** One frame may contain noise, a hand, a shadow, or blur. Consecutive evidence is more reliable than one instantaneous comparison.
7. **Event logging:** Record meaningful transitions and observations locally and, when available, send them to the backend.
   - **WHY:** The system needs an auditable history, and a temporary network failure must not erase a local observation.
8. **User feedback / reminder integration:** Display or communicate the current supported result.
   - **WHY:** Users need a useful outcome, but the wording must not be more certain than the evidence.
9. **Evaluation:** Compare system decisions with independently recorded ground truth.
   - **WHY:** A working demo is not a measurement of precision or recall.

A failure in an earlier stage must not be hidden by a later stage. For example, the state machine should not report `EXPECTED_PRESENT` if the camera failed to deliver a frame.

---

## 4. Current Repository and Development Environment

### 4.1 Workspace and runtime

The current local workspace is:

`E:\Codes\Medvfy`

The project is developed on Windows using PowerShell and a Python virtual environment:

`E:\Codes\Medvfy\.venv`

The machine's previously reported Python version was **Python 3.13.3**. The Phase 0 report listed installed versions as Flask 3.1.3, OpenCV 5.0.0.93, NumPy 2.5.4, Requests 2.34.2, and Werkzeug 3.1.9. Verify the environment before changing dependency constraints; versions may have changed since the report.

The public repository is:

`https://github.com/AnujSharma-05/Medvfy`

### 4.2 Last reported camera discovery

During reconnaissance, camera index 0 was available through DirectShow at 640×480. An earlier camera-listing script found indices 0 and 1 available at 640×480, although the later QA assessment explicitly recorded index 0. Re-test camera enumeration if the current behaviour matters.

**WHY this matters:** A camera index is an operating-system enumeration, not a permanent identity for a specific webcam. Resolution, camera index, and region coordinates must be validated together.

### 4.3 Current Git state from the latest reported QA run

The latest report said the branch was `main`, up to date with `origin/main`, with **uncommitted changes**. The report listed these modified files:

- `README.md`
- `config/settings.json`
- `vision/camera_inspector.py`
- `vision/comparator.py`
- `vision/event_logger.py`
- `vision/state_machine.py`

It also listed untracked files:

- `prompt.md`
- `tests/test_classifier.py`
- `tests/test_comparator.py`
- `tests/test_frame_quality.py`
- `tests/test_integration.py`
- `tests/test_milestone1_invariants.py`
- `tests/test_reference_store.py`
- `tests/test_roi_manager.py`
- `tests/test_scenarios.py`
- `tests/test_state_machine.py`
- `vision/__init__.py`
- `vision/classifier_interface.py`
- `vision/frame_quality.py`
- `vision/list_cameras.py`
- `vision/reference_store.py`
- `vision/roi_manager.py`

This is a **historical snapshot from the latest supplied QA report**, not a guarantee of current Git status.

**Before making any changes, run `git status --short` again.** Do not stage, commit, push, or discard changes unless explicitly instructed.

---

## 5. Architecture and Responsibility Boundaries

The latest QA report describes the following architecture. Inspect the actual files before relying on exact implementation details.

```text
Camera + Interactive UI
  vision/camera_inspector.py
       |
       +--> vision/frame_quality.py
       |      Checks whether captured frames are usable
       |
       +--> vision/roi_manager.py
       |      Validates camera-associated compartment regions
       |
       +--> vision/reference_store.py
       |      Stores expected and empty reference images separately
       |
       +--> vision/comparator.py
       |      Calculates visual-comparison evidence
       |
       +--> vision/state_machine.py
       |      Combines observations over time into state transitions
       |          |
       |          +--> data/vision_state.json
       |               Persists confirmed compartment state
       |
       +--> vision/event_logger.py
              |
              +--> data/vision_observations.csv
              +--> HTTP POST /api/observations
                         |
                         +--> backend/app.py
```

### 5.1 `vision/camera_inspector.py` — interactive coordinator

This is the live camera tool used to select regions, calibrate references, compare current images, and show results.

It should coordinate modules; it should not contain every algorithm's implementation.

**WHY:** Keeping the UI/camera loop separate from state logic allows the state machine and comparator to be tested without a physical webcam. It also prevents interface code from becoming difficult to reason about.

The latest reported controls are:

- `R` — define or redraw the compartment regions.
- `S` — save the expected reference using the current compartment contents.
- `E` — save the empty-compartment reference with the compartments empty.
- `V` — compare the latest usable observation with the stored references and evaluate state transitions.
- `C` — switch camera, if supported by the current implementation.
- `Q` — quit and release the camera.

Verify these bindings in the live code before changing or documenting them elsewhere.

### 5.2 `vision/frame_quality.py` — input quality gate

This module is reported to assess aspects such as blur, exposure, frame validity, and motion stability.

**WHY:** A low-quality frame should not be allowed to trigger a definitive medication-state transition. Quality assessment protects later stages from unreliable input.

Keep quality thresholds configurable and testable. A blur metric is only a heuristic: test it across the actual camera and container conditions. Do not assume one threshold works for every camera or lighting setup.

### 5.3 `vision/roi_manager.py` — compartment geometry

This module is reported to validate and crop configured ROIs and to associate saved regions with camera information such as index and resolution.

**WHY:** An ROI is meaningful only for the image geometry for which it was calibrated. If the camera changes or the resolution changes, an old rectangle may point to the wrong location while still being numerically valid.

A camera change or an incompatible resolution should require explicit recalibration instead of silently reusing old coordinates. Preserve the legacy flat ROI format only if compatibility behaviour is documented and tested.

### 5.4 `vision/reference_store.py` — expected and empty references

The latest report describes a `ReferenceStore` that keeps **expected** and **empty** references separate. The image archive is reported as `data/vision_baseline.npz`, with metadata in `data/vision_baseline.json`.

The reported key convention is:

- `{slot}` for the expected reference.
- `empty_{slot}` for the empty-compartment reference.

The actual implementation must be checked for its exact key names and metadata structure.

**WHY two references?**

- The expected reference answers: “Does the visible scene match the expected compartment contents?”
- The empty reference answers: “Does the scene resemble the compartment with no contents?”

These references are not interchangeable. A single mutable baseline cannot reliably represent both states.

**Core invariant:** Observation and comparison must never rewrite a calibrated reference. Only an explicit calibration action may do so.

The report says `get_expected()` returns a copy of the stored array. This helps protect the saved reference against accidental mutation. Corrupt, missing, or incompatible references should fail safely and visibly.

### 5.5 `vision/comparator.py` — visual evidence, not final truth

This module compares an observation with stored references and returns metrics used by the state machine. The latest test output includes checks for identical images, lighting shifts, object movement, and object removal.

The earlier detector used Gaussian blur, absolute pixel differences, grayscale thresholding, morphological filtering, and a changed-pixel fraction. The latest report calls the newer component a multi-metric comparator; inspect its current methods before describing it as any more sophisticated than the code supports.

**WHY keep the comparator separate?** Image-processing algorithms can be evaluated and replaced without rewriting state transitions, persistence, or the UI.

**Important limitation:** Visual similarity is evidence, not a proof of identity. A comparator may detect a large change without explaining its physical cause. Similar pills may remain visually indistinguishable under the current camera conditions.

Guard against invalid inputs, including `None`, zero-sized images, unexpected shapes, invalid metrics, NaN, and infinity. Never allow an invalid metric to bypass thresholds or generate a confident result.

### 5.6 `vision/state_machine.py` — state over time

The latest report describes a `CompartmentStateMachine` that consumes numeric metrics/state evidence and is independent of camera handles and OpenCV's window loop. It reportedly persists confirmed states in `data/vision_state.json` and supports loading them after restart.

**WHY a state machine?** An image describes one instant. Medication-related actions happen over time. The software must remember what it previously believed, distinguish a temporary candidate from a confirmed transition, and avoid emitting the same event repeatedly for an unchanged scene.

The current code may contain more states than the minimum list below. Verify the actual enum and transition rules before changing them. Relevant states described across the project include:

- `UNKNOWN`
- `EXPECTED_PRESENT`
- `REMOVAL_CANDIDATE`
- `REMOVAL_DETECTED`
- `RETURN_CANDIDATE`
- `OBJECT_RETURNED`
- `PARTIAL_OR_WRONG`
- `UNCERTAIN`
- `EMPTY` or an equivalent empty observation/state, depending on the current implementation

Do not conflate a visual observation that the compartment appears empty with the transition event that removal was detected. The current implementation reportedly retains `REMOVAL_DETECTED` when the same empty condition continues, so it does not generate a new event every frame.

A state-machine decision should make clear, directly or through its returned structure:

- previous confirmed state;
- current confirmed state;
- current candidate, if any;
- whether a new confirmed transition occurred;
- why that decision was made;
- whether the evidence is valid.

Use consecutive-frame confirmation or a similarly justified temporal policy for important transitions. Invalid evidence should clear or suspend candidate confirmation according to explicit rules, not accidentally confirm an event.

### 5.7 `vision/event_logger.py` — record meaningful events

The latest report describes an `EventLogger` that writes locally to `data/vision_observations.csv` and sends observations to `POST /api/observations` where possible.

The existing CSV compatibility contract is reported as these five columns:

```text
timestamp_utc,compartment_id,state,changed_pixel_fraction,interpretation
```

**WHY local persistence first?** The network may be unavailable. Losing the record because an HTTP request failed would destroy useful debugging evidence and interrupt the audit trail.

The logger should distinguish an observation from a newly confirmed transition. A stable scene can be observed repeatedly without representing a new event. Avoid duplicate alerts and duplicate transition events.

Keep network failures non-fatal to local logging, but make them visible in diagnostics. Keep the API payload compatible with `backend/app.py`, or deliberately update the API and tests together.

### 5.8 `vision/classifier_interface.py` and classifier tests

The latest reported tree includes `vision/classifier_interface.py` and `tests/test_classifier.py`. The tests mention a `TestClassicalHeuristicClassifier` and examples involving count mismatch, one tablet, two tablets, and an empty compartment.

**Important:** The existence of a classifier interface or passing heuristic tests does not prove that real medication identity or pill count has been solved. A heuristic may be useful for a controlled demonstration or as a preliminary estimate, but its limitations must be documented and measured.

**WHY keep an interface?** A stable contract lets the project evaluate a future classifier or object detector without tightly coupling the model to the camera UI and state machine. Do not expand this interface prematurely unless an actual next step needs it.

---

## 6. Important Data Files and Their Meanings

The latest architecture references these paths:

| Path | Purpose | Why it matters |
|---|---|---|
| `config/settings.json` | Runtime configuration: compartments, camera options, thresholds, server/device settings | Centralizes parameters so behaviour is reproducible and adjustable. |
| `config/rois.json` | Saved compartment regions, possibly with camera/resolution metadata in the current implementation | Prevents each run from requiring manual boxes, while requiring validation when camera geometry changes. |
| `data/vision_baseline.npz` | Stored image crops for expected and empty references | Keeps comparison references separate from observations. |
| `data/vision_baseline.json` | Reported reference metadata and versioning | Allows the application to identify reference type, compatibility, and calibration context. |
| `data/vision_state.json` | Reported persisted state machine data | Preserves last confirmed states across application restarts. |
| `data/vision_observations.csv` | Local tabular observation log | Supports debugging, evaluation, and backward compatibility. |
| `data/observations.jsonl` | Existing observation-related record store from earlier project context; verify current usage | May be used by another part of the application; do not delete or repurpose without tracing callers. |
| `data/events.jsonl` | Existing event-related record store from earlier project context; verify current usage | May store application events separately from the vision CSV. |

Do not assume every file currently exists or that every reported module uses precisely these paths. Inspect the current implementation and maintain one clear source of truth for each responsibility.

Do not store generated reference images, logs, or runtime state in a way that silently clobbers unrelated files. Use appropriate validation and safe file replacement where required.

---

## 7. The State Model Explained from First Principles

### 7.1 Observation, decision, and event are different things

These concepts must remain separate:

- **Observation:** What the camera and comparator measured in the current frame or frame batch.
- **Candidate:** A possible transition that has not received enough confirming evidence.
- **Confirmed state:** The latest state the software has accepted as supported by its decision rules.
- **Event:** A newly confirmed transition worth recording or notifying about.

**WHY:** If every observation becomes an event, the system will spam logs and reminders. If candidates are treated as confirmed states immediately, one noisy frame can create a false alert.

### 7.2 Example: expected contents → removed → returned

A simplified intended lifecycle is:

1. Calibration establishes the expected reference and, where possible, the empty reference.
2. Stable evidence matching the expected reference supports `EXPECTED_PRESENT`.
3. The scene appears inconsistent with the expected contents and consistent with removal/empty evidence. The system enters a removal candidate state.
4. If enough valid consecutive observations support the transition, it records `REMOVAL_DETECTED` once.
5. Continued empty observations keep the confirmed removal state and do not generate repeated removal events.
6. A scene that again matches the expected reference may enter a return candidate state.
7. After enough valid observations confirm the match, it records `OBJECT_RETURNED` as a distinct transition.
8. If a foreign object, partial amount, or ambiguous image is observed, the state machine should report the corresponding supported state or `UNCERTAIN`, not automatically declare a successful return.

This is a conceptual lifecycle. Read the actual implementation to confirm the current transition semantics and whether an explicit state-reset/calibration action is required.

### 7.3 Debouncing

Debouncing means requiring multiple consecutive qualifying observations before confirming a change. The latest report describes a configurable `debounce_frames` with a minimum of two frames for the tested policy.

**WHY:** A single frame can be disturbed by a passing hand, focus fluctuation, auto-exposure, a transient shadow, or frame noise. Several agreeing observations reduce the effect of one-off errors.

Debouncing is not magic. If frames are highly correlated or lighting changes persist, repeated frames can agree on the wrong result. Frame diversity, quality checks, good camera placement, robust comparison, and real-world evaluation are still necessary.

### 7.4 Uncertainty and recovery

When blur or occlusion makes the image unusable, do not infer that the medicine was removed or returned. Mark the observation as uncertain and preserve the last confirmed state where appropriate. Once usable evidence returns, compare it against the saved references and recover only when the evidence supports a transition.

**WHY:** An invalid observation is not evidence for either the old state or a new one. Keeping the last confirmed state separately prevents one bad frame from rewriting history.

### 7.5 Persistence across restart

The latest report says confirmed states are stored atomically in `data/vision_state.json` and loaded on restart.

**WHY:** Without persistence, a restart can forget that removal was already confirmed. When the same empty compartment is seen again, the program may emit a duplicate alert or make an inconsistent decision.

State persistence must be validated against version changes, corrupt JSON, missing files, and potentially changed references. On incompatible state data, recover safely and say that recalibration or re-evaluation is required rather than silently assuming `EXPECTED_PRESENT`.

---

## 8. Reference Management: Core Invariants

The following rules are fundamental to the project.

### Invariant 1 — An observation never overwrites calibration

Comparing a live frame with a reference must not modify the expected or empty reference, in memory or on disk.

**WHY:** If the reference automatically follows the latest observation, a removed object can become the new “normal.” The system then loses the ability to detect that removal on the next comparison.

### Invariant 2 — Expected and empty are independent

Saving or updating the expected reference must not modify the empty reference, and saving or updating the empty reference must not modify expected.

**WHY:** They represent different physical situations. Replacing one with the other destroys useful evidence.

### Invariant 3 — Calibration is explicit

Only a deliberate user action should create or replace a reference. An observation must not silently re-calibrate the system.

**WHY:** Automatic re-calibration during a suspected error can normalize the error itself.

### Invariant 4 — References must match their context

A reference must not be silently reused if its ROI, camera identity/index, image dimensions, or relevant configuration is incompatible with the current scene.

**WHY:** Comparing different regions or resolutions can create meaningless differences or accidental matches.

### Invariant 5 — Missing or corrupt calibration cannot imply a positive state

If the required reference is unavailable, the system should clearly request calibration or report an uncertain/uninitialized status.

**WHY:** Lack of evidence is not evidence that the expected medicine is present.

### Invariant 6 — Persistence operations should fail safely

Reference and state files should be validated. Where a file contains critical state, use safe writing/recovery techniques such as writing a temporary file and atomically replacing the prior file when appropriate.

**WHY:** A crash during a write should not leave a partially written file that appears valid or silently destroys the prior good state.

---

## 9. Latest QA Status (As Reported by the User-Provided QA Report)

**Latest reported gate:** Milestone 1 QA report says **PASS — approved for Milestone 1**.

The report says the following command passed:

```powershell
.\.venv\Scripts\python.exe -m py_compile (Get-ChildItem -Recurse -Include *.py vision, tests, backend).FullName
```

It also provides this full-suite summary:

```text
Ran 54 tests in 0.643s
OK
```

Reported counts: 54 tests, 54 passed, 0 failures, 0 errors, 0 skipped.

The report says the 54 tests include:

- Four backend API tests.
- Classical heuristic classifier tests.
- Visual comparator tests.
- Frame quality tests.
- An end-to-end pipeline test.
- Eight Milestone 1 invariant tests.
- Reference store tests.
- ROI manager tests.
- Twelve scenario tests.
- State machine tests.

The report states that four additional defects were discovered and fixed during QA:

1. Duplicate transition events were produced while an already-removed compartment remained empty.
2. Confirmed state was lost across process restart.
3. Recovery from `UNCERTAIN` could fall into an incorrect state.
4. Invalid images or NaN/negative metrics were not guarded sufficiently.

It reports fixes to retain the removal state without duplicate events, persist state in `data/vision_state.json`, recover based on last confirmed state, and validate image/metric inputs.

**Important verification rule:** These are facts about what the supplied report claims and shows. A future agent must re-run the suite after changes and must not present this historical result as the result of a new run. Tests can pass while the real camera still behaves poorly. The latest report explicitly leaves physical optical noise, real tablets and overlapping pills, and camera mounting/geometry unverified.

### Automated evidence versus physical validation

The reported automated tests support deterministic software behaviour using test fixtures, mocked evidence, and synthetic/scenario inputs.

They do **not** establish that:

- every real pill can be distinguished;
- small tablets are detectable under all lighting conditions;
- the actual container geometry works reliably;
- touching or overlapping pills can be counted correctly;
- the detector has production-ready precision/recall;
- medication ingestion has occurred.

**WHY:** Software tests validate the cases they exercise. Real camera behaviour includes uncontrolled factors that synthetic fixtures may not reproduce.

---

## 10. Existing API and Integration Contracts

The Phase 0 assessment listed these Flask routes. Verify the current route definitions before modifying them:

- `GET /api/health`
- `POST /api/reminders` — compartment identifier is expected to be one of `A`, `B`, `C`, `D`
- `POST /api/device/stop`
- `POST /api/observations`
- `GET /api/status`
- `GET /` — web console

The device abstraction is reported in `backend/device_adapter.py`, with `MockDevice` and `ESP32HTTPDevice` implementations.

### API and logging rules

- Keep the existing observation payload and CSV schema compatible unless an intentional migration is implemented and tested.
- Treat remote API failure as a delivery problem, not a reason to lose the local observation.
- Do not send repeated confirmed transition notifications for every stable frame.
- Do not claim an observation was delivered if the HTTP request failed.
- Do not let a backend outage crash the camera loop or silently discard a local record.

**WHY:** The vision tool must remain usable during local development and weak connectivity, while preserving a traceable record of what it observed.

### ESP32 and hardware status

A prior project stage successfully uploaded serial-command firmware to an ESP32 and tested basic LED/buzzer behaviour in a lab. However, the currently described backend adapter expects HTTP commands while the known Arduino sketch was serial-command based. Treat Wi-Fi/HTTP communication between backend and ESP32 as unimplemented unless current source code proves otherwise.

The current development phase should work on a laptop and webcam without physical ESP32 hardware.

**WHY:** The computer-vision algorithm can and should be developed and tested independently from the hardware integration. Hardware availability should not block software correctness work.

---

## 11. Development Commands

Run from the repository root in PowerShell.

### Activate the virtual environment

```powershell
.\.venv\Scripts\Activate.ps1
```

Or use the interpreter explicitly, which avoids ambiguity about which Python is running:

```powershell
.\.venv\Scripts\python.exe --version
```

### Run the full test suite

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Compile Python source files

```powershell
.\.venv\Scripts\python.exe -m py_compile (Get-ChildItem -Recurse -Include *.py vision, tests, backend).FullName
```

### Launch the camera inspector

```powershell
.\.venv\Scripts\python.exe vision/camera_inspector.py
```

### Check Git changes

```powershell
git status --short
git diff --
```

**WHY explicitly choose the virtual-environment interpreter?** It reduces the chance that tests run against a different Python installation or missing/different dependencies.

**WHY inspect Git before and after?** The pre-change status protects user work; the post-change diff helps identify unintended files, incomplete edits, or unrelated regressions.

---

## 12. Recommended Manual Validation Protocol

After automated tests pass, run the interactive camera inspector with a harmless dummy token/object rather than actual medication during early algorithm experiments.

1. Stabilize the camera, container, background, and lighting.
   - **WHY:** If the camera moves or light changes dramatically, the test measures setup variation rather than just object change.
2. Press `R` and define each compartment region carefully.
   - **WHY:** Incorrect ROIs corrupt all later comparisons.
3. Place the expected dummy objects in the right compartments and press `S` to calibrate expected references.
   - **WHY:** The detector needs an explicit representation of the intended visible contents.
4. Remove all dummy objects and press `E` to calibrate the empty references.
   - **WHY:** This gives the comparator a reference for what an empty compartment looks like.
5. Press `V` with the objects present; record the output.
   - **WHY:** The detector should not report removal from a stable scene.
6. Remove one dummy object and press `V` enough times for the configured debouncing policy; inspect the state and logs.
   - **WHY:** One transient frame must not create a confirmed event, but sustained evidence should eventually do so.
7. Leave the compartment empty and press `V` again; confirm the same removal event is not repeatedly emitted.
   - **WHY:** A stable state should not continuously trigger new notifications.
8. Return the same dummy object to its expected place and press `V` repeatedly; check for a distinct return transition.
   - **WHY:** Return requires evidence matching the expected reference, not merely the presence of any object.
9. Put an unrelated object in the compartment and repeat.
   - **WHY:** The system must not treat any object as the expected item.
10. Introduce a temporary hand occlusion, slight lighting change, and mild blur, one at a time.
    - **WHY:** These tests reveal whether quality checks and uncertainty handling prevent false confident results.
11. Quit and restart the application. Check that reference and confirmed-state files still exist and can be loaded.
    - **WHY:** Process lifecycle must not erase calibration or refire an already-confirmed event.
12. Inspect the local CSV and any backend logs, and compare them with a handwritten ground-truth record of what was actually done.
    - **WHY:** Accuracy can only be assessed by comparing predictions against independent truth.

For every manual experiment, record the lighting/camera setup, compartment, expected condition, actual physical action, software result, and whether a false positive or false negative occurred. Repeat each scenario several times before drawing conclusions.

---

## 13. Computer Vision: Current Reality and Future Direction

### 13.1 Why one pixel threshold is not enough

A simple changed-pixel fraction is useful as an initial signal but does not identify the physical cause of a difference. Lighting, shadows, camera vibration, object movement, and actual removal all affect pixels.

**WHY not just lower the threshold?** A lower threshold may catch smaller changes but also increases false positives. A higher threshold can reduce noise but miss genuine small objects or partial changes. There is no universally correct threshold independent of setup and data.

### 13.2 Possible techniques to evaluate later

Depending on physical tests and available data, evaluate:

- frame stabilization and quality rejection;
- illumination normalization;
- structural similarity or feature-based comparison;
- edges, foreground masks, contours, and connected components;
- classical object/count heuristics in carefully controlled scenes;
- object detection or instance segmentation;
- a medication-specific classifier or visual-reference matching;
- pill imprint recognition if markings can be imaged at sufficient resolution.

Do not implement every technique by default. Measure the simplest candidate methods first.

**WHY use empirical selection?** More complex models add dependencies, runtime cost, new failure modes, and data requirements. Complexity is useful only when it produces measurable gains for the target conditions.

### 13.3 Medication classification requires suitable data

To classify known medications, the project will likely need labelled samples of the actual target classes, representative images across orientations and lighting, and a held-out evaluation set. It must define which differences matter: product identity, strength, dosage form, or another category.

Do not assume a general-purpose image model can reliably distinguish pills photographed at 640×480 through a compartment lid or at an unknown distance.

**WHY data first?** Model choice is only one part of the problem. Resolution, angle, lighting, visual similarity, label quality, and coverage of the real use cases often determine what accuracy is possible.

### 13.4 Metrics to measure

When labelled observations exist, evaluate:

- precision and recall for removal detection;
- false-positive and false-negative rates;
- F1 score;
- confusion matrix across states/scenarios;
- number/count accuracy if true counts are labelled;
- identity accuracy only if medication labels exist;
- `UNCERTAIN` frequency;
- inference and end-to-end latency;
- performance by compartment and lighting condition.

Report dataset size and how train/validation/test sets were separated. Keep test observations independent from calibration or threshold tuning where feasible.

**WHY accuracy alone is not enough:** A highly imbalanced dataset can produce an impressive accuracy score while missing the event that matters. False positives and false negatives have different practical consequences and need separate reporting.

---

## 14. Practical Engineering Principles for Sustainable Development

### 14.1 Keep dependencies deliberate

Do not install a new computer-vision or machine-learning library without explaining what limitation it addresses and how it will be evaluated.

**WHY:** Every dependency increases installation complexity, compatibility risk, maintenance, and sometimes runtime requirements.

### 14.2 Make thresholds reproducible

Put important adjustable values in a validated configuration or a clearly defined algorithm configuration object. Record relevant threshold/configuration versions in evaluation output where useful.

**WHY:** Scattered magic numbers make behaviour difficult to reproduce and compare across experiments.

### 14.3 Keep algorithms testable without hardware

Camera acquisition, comparison, state logic, and event persistence should have testable boundaries. Synthetic images and mocked evidence should exercise deterministic logic.

**WHY:** A lab camera may be unavailable, occupied, or unreliable. Development must not stop merely because hardware is disconnected.

### 14.4 Use tests to prevent regressions

When a defect is fixed, add a test that would have failed before the fix. Keep tests small enough to make failures understandable.

**WHY:** Without a regression test, the same defect can reappear during the next refactor or feature addition.

### 14.5 Preserve interfaces unless change is justified

If a function signature, JSON format, CSV schema, state enum, or API contract changes, trace all callers and update tests and documentation at the same time.

**WHY:** Interface changes can break the rest of the system even when the modified module's unit tests pass.

### 14.6 Log reasons as well as results

For important decisions, make it possible to explain why the system concluded that a change was confirmed or uncertain. Log measurements and quality reasons where useful without retaining unnecessary personal or sensitive imagery.

**WHY:** A state without its supporting evidence is difficult to debug, audit, or improve.

### 14.7 Avoid accidental state normalization

Do not automatically update references when an object disappears or reappears. Require an explicit calibration action for changes to the expected setup.

**WHY:** Otherwise the system may learn a mistake as normal and make subsequent checks less reliable.

### 14.8 Keep demo success and product readiness separate

A clean demo, a complete test suite, or a passing synthetic scenario is progress, but not proof of field reliability.

**WHY:** Real-world lighting, enclosure geometry, motion, pill variation, and user behaviour create conditions not represented in a small test suite.

---

## 15. Project Roadmap

The project should advance through gated milestones. Do not skip a gate because a new technique sounds promising.

### Milestone 1 — Reference integrity and state management

Reported as passed by the latest supplied QA report.

Goals:

- independent expected and empty references;
- no automatic reference overwrite;
- explicit state machine;
- temporal confirmation/debouncing;
- duplicate event suppression;
- state persistence across restarts;
- safe handling of invalid images/metrics;
- unit and invariant tests.

**WHY this milestone comes first:** A sophisticated detector cannot compensate for a broken reference model or incorrect state persistence.

### Milestone 2 — Real camera acquisition and calibration reliability

Goals:

- test actual camera selection and resolution;
- validate frame quality under real conditions;
- validate or improve ROI/camera metadata;
- test reconnect/failure behaviour;
- document mounting and lighting recommendations.

**WHY:** The algorithm operates on images the hardware can actually capture. Resolution, blur, reflections, focus, and geometry establish the ceiling for attainable accuracy.

### Milestone 3 — Robust visual state evidence

Goals:

- evaluate comparison metrics with captured data;
- separate movement from removal where possible;
- detect partial changes and foreign objects within validated limits;
- tune thresholds only using labelled observations;
- measure false positives and false negatives.

**WHY:** The current state machine can manage evidence, but it cannot make poor visual features informative.

### Milestone 4 — Count and medication identification feasibility

Goals:

- define target medication categories and required distinction level;
- collect labelled, representative images;
- establish training/validation/test procedures;
- evaluate count estimation and identity separately;
- retain uncertainty when visually similar medications cannot be distinguished.

**WHY:** Object count and medication identity are different tasks and require ground truth for credible evaluation.

### Milestone 5 — Integration and release readiness

Goals:

- verify vision/API/device integration;
- verify logging and restart behaviour;
- evaluate normal and failure paths;
- run regression and scenario tests;
- document setup, calibration, safe use, limitations, and recovery.

**WHY:** A feature is only ready when its behaviour remains understandable and safe across the full workflow, not just inside one isolated module.

### Later hardware integration

When lab access is available, resume the ESP32 path separately. First confirm current firmware protocol and backend device adapter expectations. Avoid mixing camera algorithm changes with hardware protocol changes in the same task unless integration itself is the objective.

**WHY:** Separating concerns makes failures easier to localize: a visual-classification issue should not be confused with a Wi-Fi or microcontroller issue.

---

## 16. Suggested Work Pattern for the AI Agent

For each task, respond to yourself in this order:

1. **What must be true at the end?** Express the goal as observable behaviour.
2. **Why is the current behaviour insufficient?** Identify the root cause, not just the symptom.
3. **Which existing module owns this responsibility?** Prefer using a clear boundary over duplicating code.
4. **What is the smallest appropriate change?** Avoid broad refactoring.
5. **What test would fail before the fix and pass after it?** Add or update it.
6. **What existing contracts could this affect?** Check configuration, CSV, API, persistence, UI, and other callers.
7. **What did the tests actually show?** Run the relevant tests and the full suite when appropriate.
8. **What remains unverified?** State physical-camera or data limitations explicitly.
9. **What files changed?** Inspect Git diff/status and report the exact files.

For multi-step tasks, begin with a short plan, but do not use planning as a substitute for implementation. Give a factual final report that separates completed, tested, partially completed, and unverified work.

---

## 17. Definition of Done for a Meaningful Change

A code task should not be called complete until the relevant items below have been considered:

- [ ] Root cause identified and documented.
- [ ] Existing code and Git state inspected before editing.
- [ ] Change implemented at the appropriate module boundary.
- [ ] Regression test added or updated.
- [ ] Targeted tests run.
- [ ] Full test suite run when changes could affect integration.
- [ ] Configuration and data compatibility checked.
- [ ] Invalid input and failure behaviour considered.
- [ ] User-facing state names and wording remain truthful.
- [ ] Diff reviewed for accidental or unrelated changes.
- [ ] Manual hardware/camera requirements identified.
- [ ] Test results and remaining limits reported honestly.

Not every checkbox applies to every one-line change, but the agent must consciously assess the relevant ones.

---

## 18. Final Operating Principle

MedVerify must not be designed to say something confidently. It must be designed to say only what the available evidence supports.

The system's real value is not the number of computer-vision techniques it uses. Its value comes from clear references, good image acquisition, stable state transitions, robust persistence, reproducible testing, honest uncertainty, and measured performance on real conditions.

**Before trying to make the detector smarter, make sure the evidence it receives is valid, the reference it compares against is correct, the state it remembers is coherent, and the result it reports is truthful.**
