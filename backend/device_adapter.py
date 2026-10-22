"""Device adapters keep the backend independent of physical hardware.

Modes (settings.json "device_mode", overridden by env MEDVERIFY_DEVICE):
    mock    Software only; prints what the hardware would do. Default.
    serial  ESP32 over USB serial, speaking the existing MedVerify_Controller.ino
            protocol ("A".."D" to light a compartment and beep, "STOP" to clear).
            Port from settings "serial_port" or env MEDVERIFY_SERIAL_PORT (e.g. COM5).
    esp32   ESP32 over Wi-Fi HTTP (POST /command). The current firmware does NOT
            implement this yet; kept for a future Wi-Fi firmware.
"""
from __future__ import annotations
import os
import threading
import time
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


class ESP32SerialDevice:
    """ESP32 running MedVerify_Controller.ino, connected by USB cable.

    The firmware lights one LED at a time: a new reminder turns the others off.
    """
    mode = "serial"
    BOOT_WAIT_S = 2.0  # Opening the port can reset the board; wait for it to boot

    def __init__(self, port: str, baudrate: int = 115200, serial_factory: Any = None) -> None:
        if not port:
            raise ValueError("Serial port is required (settings 'serial_port' or MEDVERIFY_SERIAL_PORT)")
        self.port = port
        self.baudrate = baudrate
        self._serial_factory = serial_factory
        self._conn: Any = None
        self._lock = threading.Lock()
        self.active_compartment: str | None = None
        self.last_error: str | None = None
        self.last_response: str | None = None

    def _connection(self) -> Any:
        if self._conn is not None and getattr(self._conn, "is_open", True):
            return self._conn
        if self._serial_factory is not None:
            self._conn = self._serial_factory(self.port, self.baudrate)
            return self._conn
        import serial  # pyserial; imported lazily so other modes do not need it

        conn = serial.Serial()
        conn.port = self.port
        conn.baudrate = self.baudrate
        conn.timeout = 0.3
        conn.write_timeout = 1.0
        conn.dtr = False  # Avoid auto-reset where the USB bridge allows it
        conn.rts = False
        conn.open()
        time.sleep(self.BOOT_WAIT_S)
        conn.reset_input_buffer()
        self._conn = conn
        return conn

    def _send(self, command: str) -> str:
        with self._lock:
            try:
                conn = self._connection()
                conn.write((command + "\n").encode("ascii"))
                conn.flush()
                response = conn.readline().decode("utf-8", "replace").strip()
                self.last_response = response
                self.last_error = None
                return response
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    if self._conn is not None:
                        self._conn.close()
                finally:
                    self._conn = None  # Reconnect on the next command
                raise

    def trigger_reminder(self, compartment_id: str, event_id: str) -> dict[str, Any]:
        response = self._send(compartment_id.upper())
        self.active_compartment = compartment_id.upper()
        return {"ok": True, "mode": self.mode, "response": response}

    def stop_alert(self) -> dict[str, Any]:
        response = self._send("STOP")
        self.active_compartment = None
        return {"ok": True, "mode": self.mode, "response": response}

    def get_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "port": self.port,
            "online": self._conn is not None and self.last_error is None,
            "active_compartment": self.active_compartment,
            "last_response": self.last_response,
            "last_error": self.last_error,
        }


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


def build_device(settings: dict[str, Any] | None = None):
    settings = settings or {}
    mode = (os.getenv("MEDVERIFY_DEVICE") or settings.get("device_mode") or "mock").strip().lower()
    if mode == "serial":
        port = os.getenv("MEDVERIFY_SERIAL_PORT") or settings.get("serial_port") or ""
        baud = int(settings.get("serial_baudrate", 115200))
        return ESP32SerialDevice(port, baud)
    if mode == "esp32":
        base_url = os.getenv("MEDVERIFY_ESP32_URL") or settings.get("esp32_base_url") or "http://192.168.4.1"
        return ESP32HTTPDevice(base_url)
    return MockDevice()
