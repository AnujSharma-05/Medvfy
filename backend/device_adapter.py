"""Device adapters keep the backend independent of physical hardware.

Modes (settings.json "device_mode", overridden by env MEDVERIFY_DEVICE):
    mock    Software only; prints what the hardware would do. Default.
    serial  ESP32 over USB serial, speaking the existing MedVerify_Controller.ino
            protocol ("A".."D" to light a compartment and beep, "STOP" to clear).
            Port from settings "serial_port" or env MEDVERIFY_SERIAL_PORT (e.g. COM28),
            or "auto" to find the board by its USB bridge chip (COM numbers can change).
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

    def silence(self) -> dict[str, Any]:
        """Buzzer off, compartment light stays on."""
        self.state.update({"buzzer_on": False})
        print(f"[MOCK DEVICE] Buzzer silenced; LED {self.state['active_compartment']} stays ON")
        return {"ok": True, "mode": self.mode, "device_state": self.state.copy()}

    def sound(self, kind: str) -> dict[str, Any]:
        """'alarm' (too many pills) or 'warn' (advisory). Lights unchanged."""
        self.state["last_command"] = {"type": kind.upper()}
        print(f"[MOCK DEVICE] Buzzer {kind.upper()} pattern")
        return {"ok": True, "mode": self.mode, "device_state": self.state.copy()}

    def get_state(self) -> dict[str, Any]:
        return {"mode": self.mode, **self.state}


# USB-to-serial bridge chips used on ESP32 dev boards (vendor id -> name)
ESP32_USB_BRIDGES = {0x10C4: "CP210x", 0x1A86: "CH340", 0x0403: "FTDI", 0x303A: "Espressif USB"}


def find_esp32_ports() -> list[str]:
    """Serial ports whose USB bridge chip is one used on ESP32 boards."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports() if p.vid in ESP32_USB_BRIDGES]


class DeviceNotResponding(RuntimeError):
    """The port opened but the firmware did not answer as expected."""


class ESP32SerialDevice:
    """ESP32 running MedVerify_Controller.ino, connected by USB cable.

    The firmware lights one LED at a time: a new reminder turns the others off.
    Every command must be acknowledged by the firmware's reply line; a port that opens
    but stays silent is reported as an error, never as a delivered reminder.
    port="auto" finds the board by its USB bridge chip on each (re)connect, because
    Windows can give the same board a different COM number after replugging.
    """
    mode = "serial"
    BOOT_WAIT_S = 2.0      # Opening the port can reset some boards; wait for them to boot
    REPLY_TIMEOUT_S = 1.5  # The firmware answers within milliseconds; this is generous
    EXPECTED_REPLY = {"STOP": "Reminder cleared", "HELP": "MedVerify", "ALARM": "ALARM:", "WARN": "WARN:"}

    def __init__(self, port: str, baudrate: int = 115200, serial_factory: Any = None) -> None:
        if not port:
            raise ValueError("Serial port is required (settings 'serial_port' or MEDVERIFY_SERIAL_PORT); "
                             "use \"auto\" to detect the ESP32")
        self.port_setting = port
        self.port: str | None = None if port.lower() == "auto" else port
        self.baudrate = baudrate
        self._serial_factory = serial_factory
        self._conn: Any = None
        self._lock = threading.Lock()
        self.active_compartment: str | None = None
        self.last_error: str | None = None
        self.last_response: str | None = None

    def _resolve_port(self) -> str:
        if self.port_setting.lower() != "auto":
            return self.port_setting
        found = find_esp32_ports()
        if len(found) != 1:
            raise DeviceNotResponding(
                "No ESP32 USB port found. Is the board plugged in?" if not found else
                f"Several ESP32-like ports found ({', '.join(found)}); set serial_port to one of them.")
        return found[0]

    def _connection(self) -> Any:
        if self._conn is not None and getattr(self._conn, "is_open", True):
            return self._conn
        self.port = self._resolve_port()
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
        expect = self.EXPECTED_REPLY.get(command, f"REMINDER: compartment {command}")
        with self._lock:
            try:
                fresh = self._conn is None
                conn = self._connection()
                # A board that was just (re)connected may still be booting (seen on COM28 right after
                # another program released the port): give a fresh connection one resend.
                for attempt in range(2 if fresh else 1):
                    if attempt:
                        time.sleep(self.BOOT_WAIT_S)
                    if hasattr(conn, "reset_input_buffer"):
                        conn.reset_input_buffer()  # Drop leftover lines so we read THIS command's reply
                    conn.write((command + "\n").encode("ascii"))
                    conn.flush()
                    deadline = time.monotonic() + self.REPLY_TIMEOUT_S
                    while time.monotonic() < deadline:
                        line = conn.readline().decode("utf-8", "replace").strip()
                        if expect in line:
                            self.last_response = line
                            self.last_error = None
                            return line
                raise DeviceNotResponding(f"No reply from the pill box on {self.port} to '{command}'. "
                                          "Is the MedVerify firmware running, and the Arduino Serial Monitor closed?")
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    if self._conn is not None:
                        self._conn.close()
                finally:
                    self._conn = None  # Reconnect on the next command
                raise

    def diagnose(self, timeout: float = 20.0) -> list[str]:
        """Run the firmware's DIAG sequence (~12 s of lights and a long tone); return its report."""
        with self._lock:
            try:
                conn = self._connection()
                if hasattr(conn, "reset_input_buffer"):
                    conn.reset_input_buffer()
                conn.write(b"DIAG\n")
                conn.flush()
                lines: list[str] = []
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    line = conn.readline().decode("utf-8", "replace").strip()
                    if line:
                        lines.append(line)
                    if line == "DIAG done":
                        self.last_error = None
                        return lines
                if not any(l.startswith("DIAG") for l in lines):
                    raise DeviceNotResponding("The board did not run DIAG: it needs the updated firmware "
                                              "(`mv firmware upload`).")
                raise DeviceNotResponding("DIAG did not finish: " + " | ".join(lines[-3:]))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    if self._conn is not None:
                        self._conn.close()
                finally:
                    self._conn = None
                raise

    def check(self) -> dict[str, Any]:
        """Ask the firmware to identify itself (HELP). No LEDs or sound."""
        try:
            self._send("HELP")
            return {"ok": True, "port": self.port}
        except Exception as exc:
            return {"ok": False, "port": self.port, "error": f"{type(exc).__name__}: {exc}"}

    def trigger_reminder(self, compartment_id: str, event_id: str) -> dict[str, Any]:
        response = self._send(compartment_id.upper())
        self.active_compartment = compartment_id.upper()
        return {"ok": True, "mode": self.mode, "response": response}

    def stop_alert(self) -> dict[str, Any]:
        response = self._send("STOP")
        self.active_compartment = None
        return {"ok": True, "mode": self.mode, "response": response}

    def sound(self, kind: str) -> dict[str, Any]:
        """'alarm' (too many pills) or 'warn' (advisory). Lights unchanged."""
        response = self._send({"alarm": "ALARM", "warn": "WARN"}[kind])
        return {"ok": True, "mode": self.mode, "response": response}

    def get_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "port": self.port or self.port_setting,
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
