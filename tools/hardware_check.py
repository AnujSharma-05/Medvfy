"""Guided check of the MedVerify pill box (ESP32 + 4 LEDs + buzzer) over USB.

    .venv\\Scripts\\python.exe tools\\hardware_check.py            (or run_hardware_check.bat)
    .venv\\Scripts\\python.exe tools\\hardware_check.py --port COM28

Lights each compartment in turn and asks what you see, then suggests the likely
wiring fault for anything that is off (per docs/hardware_connections.md). At the end it
can switch MedVerify to the real box (device_mode "serial", serial_port "auto").
Close the Arduino Serial Monitor first: only one program can use the port at a time.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.device_adapter import ESP32SerialDevice, find_esp32_ports  # noqa: E402

SETTINGS = ROOT / "config" / "settings.json"
GPIO = {"A": 16, "B": 17, "C": 18, "D": 19}

TIPS = {
    "no_led": "LED {slot} (GPIO{gpio}) did not light: check the LED is the right way round (long leg towards "
              "the resistor/GPIO{gpio}), that its resistor is in series, and that its short leg reaches the GND rail "
              "that is wired to an ESP32 GND pin.",
    "wrong_led": "Another LED lit instead of {slot}: the jumper meant for GPIO{gpio} sits on a different pin. Use "
                 "the GPIO number printed on the board (P{gpio}), not the pin's position along the header.",
    "no_beep": "No beep: buzzer S must go to GPIO25 and '-' to GND; the middle pin stays unconnected. If S and '-' "
               "are swapped it stays silent.",
    "stuck": "An LED stayed on after STOP: two LED legs or jumpers probably share one breadboard row, or an LED "
             "is wired to 3V3/5V instead of a GPIO. Unplug USB before fixing.",
}


def ask(question: str, options: dict[str, str]) -> str:
    keys = "/".join(options)
    while True:
        answer = input(f"{question} [{keys}] ").strip().lower()
        if answer in options:
            return options[answer]
        print(f"  Please type one of: {keys}")


def run(port: str, answer=ask, pause: float = 1.2) -> dict:
    """Interactive check. Returns {"ok": bool, "problems": [...], "port": ...}."""
    dev = ESP32SerialDevice(port)
    hello = dev.check()
    if not hello["ok"]:
        return {"ok": False, "port": hello.get("port"), "problems": [
            f"The board did not answer: {hello['error']}",
            "Close the Arduino Serial Monitor (or any MedVerify server window), replug the USB cable, and "
            "re-upload MedVerify_Controller.ino if this persists."]}
    print(f"Pill box firmware answered on {hello['port']}.\n")

    problems: list[str] = []
    for slot, gpio in GPIO.items():
        dev.trigger_reminder(slot, "hardware-check")
        time.sleep(pause)  # let the 3-beep pattern finish before asking
        seen = answer(f"Compartment {slot}: is ONLY LED {slot} on, and did you hear 3 beeps?",
                      {"y": "ok", "l": "no_led", "w": "wrong_led", "b": "no_beep"})
        if seen != "ok":
            problems.append(TIPS[seen].format(slot=slot, gpio=gpio))
    dev.stop_alert()
    time.sleep(0.3)
    if answer("STOP sent: are all four LEDs off now?", {"y": "ok", "n": "stuck"}) != "ok":
        problems.append(TIPS["stuck"])
    if dev._conn is not None:
        dev._conn.close()
    # The same beep fault shows up four times: report it once.
    return {"ok": not problems, "port": hello["port"], "problems": list(dict.fromkeys(problems))}


def save_serial_settings(path: Path = SETTINGS) -> None:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    settings["device_mode"] = "serial"
    settings["serial_port"] = "auto"
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto", help="COM port, or 'auto' to find the ESP32 (default)")
    args = ap.parse_args()

    found = find_esp32_ports()
    print(f"ESP32-like USB ports: {', '.join(found) or 'none found'}")
    if args.port == "auto" and not found:
        print("Plug in the ESP32 with a data-capable USB cable, then run this again.")
        raise SystemExit(1)
    print("Answer each question with: y = yes, l = LED did not light, w = wrong LED lit, b = no beep.\n")

    result = run(args.port)
    print()
    if result["ok"]:
        print("All four compartments and the buzzer work.")
    else:
        print("Problems found:")
        for p in result["problems"]:
            print(f"  - {p}")
        print("Unplug USB before changing any wiring, then run this check again.")

    if result["ok"] or result["problems"] and "did not answer" not in result["problems"][0]:
        current = json.loads(SETTINGS.read_text(encoding="utf-8")).get("device_mode") if SETTINGS.exists() else None
        if current != "serial":
            if ask("Use the real pill box in MedVerify from now on (device_mode = serial, port auto)?",
                   {"y": "y", "n": "n"}) == "y":
                save_serial_settings()
                print(f"Saved to {SETTINGS}. Restart the MedVerify server to use the box.")
        else:
            print("MedVerify is already set to use the real pill box.")


if __name__ == "__main__":
    main()
