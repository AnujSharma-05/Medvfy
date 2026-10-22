

# MEDVERIFY — EXECUTION PROMPT: MILESTONE 1

You have completed Phase 0 reconnaissance and produced the technical assessment. Now IMPLEMENT Milestone 1 directly in the existing codebase.

**Operating mode:** Senior Software Engineer. Be fast, focused, test-driven, and conservative with existing code. Do not repeat the assessment, produce another roadmap, or ask for approval unless blocked by a genuinely essential decision.

## Objective

Fix the baseline overwrite defect and implement reliable, independently testable compartment state tracking for visible object removal and return.

Work only on the foundation required for Milestone 1. Do not implement a deep-learning classifier, redesign the frontend, integrate new hardware functionality, or refactor unrelated modules.

## Step 1 — Protect existing work

1. Inspect `git status` and the current implementation.
2. Preserve every existing user modification, including untracked files such as `prompt.md` and `vision/list_cameras.py`.
3. Do not reset, clean, revert, commit, or push changes.
4. Re-run the existing test suite to establish the current result.

## Step 2 — Fix reference management

Inspect `vision/camera_inspector.py`.

Implement the minimum clean design needed to keep these concepts separate:

- **Expected reference:** the explicitly calibrated expected medication appearance. Comparison must never overwrite it.
- **Empty reference:** the explicitly calibrated empty-compartment appearance.
- **Latest observation:** the current captured image and its measured evidence.
- **Confirmed state:** the last state accepted by the state machine.

Persist reference images safely. Use a small, focused `vision/reference_store.py` module if it helps separate responsibilities.

Requirements:

- Saving an expected or empty reference must be an explicit operation.
- Comparing images must never update or replace either reference.
- Missing, corrupt, or incompatible references must be handled explicitly.
- Preserve compatibility with existing configuration and current camera-inspector controls wherever possible.
- Do not silently reinterpret an old baseline as both an expected and an empty reference.
- If calibration is incomplete, show a clear message and return an uncertain or uncalibrated result.

Prefer a minimal compatible change over a broad rewrite.

## Step 3 — Implement a testable state machine

Create `vision/state_machine.py`.

Keep it independent of OpenCV camera acquisition, files, the Flask server, and other hardware.

Use explicit states appropriate to the available evidence, including at least:

- `UNKNOWN`
- `EXPECTED_PRESENT`
- `REMOVAL_CANDIDATE`
- `REMOVAL_DETECTED`
- `RETURN_CANDIDATE`
- `OBJECT_RETURNED`
- `UNCERTAIN`

Define a clear function or class API that accepts the current observation evidence and returns a decision containing the new state, previous state, whether a confirmed transition occurred, and a reason.

The machine must:

1. Keep the last confirmed state separate from a temporary candidate.
2. Require configurable consecutive qualifying observations before confirming removal or return.
3. Avoid emitting duplicate transition events for an unchanged scene.
4. Allow a confirmed removed object to subsequently return.
5. Never infer a return merely because the image changed; evidence must be consistent with the expected reference.
6. Return `UNCERTAIN` when evidence is missing, conflicting, or insufficient.
7. Never claim that visual removal proves ingestion.

Use deterministic transitions and a small, documented API. Do not add unnecessary abstractions.

## Step 4 — Integrate carefully

Connect the new reference management and state machine to `vision/camera_inspector.py`.

Preserve the existing keys and backend observation contract unless a change is necessary.

Keep the current pixel-difference detector as an initial evidence source for this milestone. Clearly label its limitations. Do not pretend it identifies pill identity or quantity.

If the current UI cannot safely support both reference calibrations without a larger redesign, make the smallest clear, documented change to the controls and prompts.

A failed camera read, missing reference, or invalid comparison must not silently become `EXPECTED_PRESENT` or `OBJECT_RETURNED`.

Preserve local event and CSV logging compatibility. Record meaningful transitions without repeatedly creating identical transition events.

## Step 5 — Add regression tests

Create focused tests under `tests/`, using synthetic images or mocked evidence where appropriate so the tests do not require a physical webcam.

At minimum test:

1. Expected reference remains unchanged after multiple comparisons.
2. Empty and expected references are stored independently.
3. Missing or corrupt references are handled safely.
4. A stable expected state produces no repeated removal events.
5. A single noisy observation does not immediately confirm removal when debouncing is configured.
6. Sustained removal evidence confirms removal after the configured number of observations.
7. Sustained evidence matching the expected state can confirm a return.
8. Repeated observations after a confirmed transition do not create duplicate transition events.
9. Invalid evidence results in `UNCERTAIN` and does not falsely confirm a return.
10. The state machine is testable without a webcam or network connection.

Keep the existing API tests passing. Do not fabricate test results or tune the implementation solely to satisfy unrealistic synthetic fixtures.

## Step 6 — Verify and report

Run:

```powershell
python -m unittest discover -s tests -v
```

Also inspect the final diff and Git status.

Fix failures introduced by your changes. If any test cannot run, state why.

### Acceptance criteria

Milestone 1 is complete only if:

- The expected reference is no longer overwritten during comparison.
- Expected and empty references are independent.
- Removal and return are distinct state transitions.
- Temporal confirmation works deterministically.
- Duplicate transition events are prevented.
- Uncertain evidence cannot be converted into a confident medication-state claim.
- Existing API tests pass.
- New tests pass.
- Unrelated changes remain untouched.

### Final response format

Return only:

1. **Implemented:** key changes and file paths.
2. **Test results:** actual command and pass/fail counts.
3. **Manual test:** exact steps to validate removal, return, and reference persistence through the webcam UI.
4. **Remaining limitations:** what is still required for reliable pill counting or medication identification.
5. **Git status:** changed files and confirmation that no commit or push was performed.

Start now. Inspect, implement, test, and report. Do not stop after proposing code.
