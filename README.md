# MedVerify — laptop-only modular starter

This workspace is designed for software development before physical hardware is available.

## Modules
- `vision/camera_inspector.py`: fixed ROI selection, before/after visual-change comparison, CSV logging; optionally posts observations to the local server.
- `backend/app.py`: Flask API and simple mobile-friendly local console.
- `backend/device_adapter.py`: `MockDevice` for tonight and an `ESP32HTTPDevice` adapter for later hardware integration.
- `config/settings.json`: central camera, compartment, and device settings.
- `tests/test_api.py`: automated tests that do not need hardware.
- `firmware/README.md`: planned ESP32 command contract.

## Windows setup (PowerShell)
Run each command separately from this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` for this terminal only, then activate again.

## Start the server in terminal 1
```powershell
python -m backend.app
```
Open `http://127.0.0.1:5000`. Leave device mode as `mock`.

## Test the server in terminal 2
```powershell
python -m unittest discover -s tests -v
```

## Start camera inspection in terminal 3
```powershell
python vision/camera_inspector.py
```
On first run, draw the ROIs for compartments A, B, C, D and press Enter/Space to accept each one. Press `S` to save a reference image of all compartment ROIs, change the arrangement using dummy tokens, then press `V` to compare and log visual changes. `R` redraws ROIs and `Q` quits.

## Current limitations
The vision module detects visual change, not pill identity, precise pill count, or ingestion. It is for controlled software testing with dummy tokens until it has been validated. Medication schedule correctness and pill identity must not rely solely on color/shape recognition. The server is development-only and bound to localhost; it is not exposed to the internet and does not yet send mobile push notifications.
