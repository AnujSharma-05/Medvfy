"""Device adapters keep the backend independent of physical hardware."""
from __future__ import annotations
import os
from typing import Any
import requests


class MockDevice:
    """Software-only controller used while hardware is unavailable."""
    mode = "mock"

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "online": True,
            "active_compartment": None,
            "buzzer_on": False,
            "last_command": None,
        }

    def trigger_reminder(self, compartment_id: str, event_id: str) -> dict[str, Any]:
        self.state.update({
            "active_compartment": compartment_id,
            "buzzer_on": True,
            "last_command": {"type": "REMINDER", "compartment_id": compartment_id, "event_id": event_id},
        })
        print(f"[MOCK DEVICE] Reminder: compartment {compartment_id}; buzzer=ON; LED={compartment_id}")
        return {"ok": True, "mode": self.mode, "device_state": self.state.copy()}

    def stop_alert(self) -> dict[str, Any]:
        self.state.update({"active_compartment": None, "buzzer_on": False})
        print("[MOCK DEVICE] Alert stopped; all compartment LEDs OFF")
        return {"ok": True, "mode": self.mode, "device_state": self.state.copy()}

    def get_state(self) -> dict[str, Any]:
        return {"mode": self.mode, **self.state}


class ESP32HTTPDevice:
    """Adapter for a future ESP32 HTTP endpoint; safe to enable only after firmware is ready."""
    mode = "esp32"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.last_command: dict[str, Any] | None = None

    def trigger_reminder(self, compartment_id: str, event_id: str) -> dict[str, Any]:
        payload = {"type": "REMINDER", "compartment_id": compartment_id, "event_id": event_id}
        response = requests.post(f"{self.base_url}/command", json=payload, timeout=3)
        response.raise_for_status()
        self.last_command = payload
        try:
            body = response.json()
        except ValueError:
            body = {"response": response.text[:200]}
        return {"ok": True, "mode": self.mode, "response": body}

    def stop_alert(self) -> dict[str, Any]:
        response = requests.post(f"{self.base_url}/command", json={"type": "STOP"}, timeout=3)
        response.raise_for_status()
        return {"ok": True, "mode": self.mode, "response": response.text[:200]}

    def get_state(self) -> dict[str, Any]:
        return {"mode": self.mode, "online": None, "last_command": self.last_command}


def build_device():
    mode = os.getenv("MEDVERIFY_DEVICE", "mock").strip().lower()
    if mode == "esp32":
        base_url = os.getenv("MEDVERIFY_ESP32_URL", "http://192.168.4.1")
        return ESP32HTTPDevice(base_url)
    return MockDevice()
