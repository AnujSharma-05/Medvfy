# Firmware integration note

Keep your current `controller.ino` as a backup. Do not wire hardware until the physical parts and labels are confirmed.

Tomorrow's firmware contract should implement:
- `POST /command` with JSON `{ "type": "REMINDER", "compartment_id": "A", "event_id": "..." }`
- `POST /command` with JSON `{ "type": "STOP" }`
- One configurable GPIO per compartment indicator and a configurable buzzer output.

Do not choose GPIOs only by convenience: confirm the exact ESP32 board, avoid boot-strapping/reserved pins where relevant, and confirm the buzzer module's voltage and drive requirements. The backend defaults to MOCK mode and makes no hardware calls unless `MEDVERIFY_DEVICE=esp32` is set.
