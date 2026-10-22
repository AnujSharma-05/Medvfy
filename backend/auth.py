"""Caretaker login for MedVerify.

One caretaker account (decision 2 in docs/flow-map.html): a password set once, on
this laptop, and stored only as a salted hash in config/auth.json together with the
random key that signs session cookies. The password can only be created from the
laptop itself, so nobody reaching the app through a public link can claim it first.

Repeated wrong passwords lock that client out for a while, because once the app has
a public link the password is the only thing between the internet and the controls.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8


class AuthError(ValueError):
    """Problem the user can fix; the message is safe to show."""


class CaretakerAuth:
    def __init__(self, path: Path | str, max_failures: int = 5, lockout_seconds: float = 300.0) -> None:
        self.path = Path(path)
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        if not self._data.get("secret_key"):
            self._data["secret_key"] = secrets.token_hex(32)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, suffix=".json", delete=False,
                                         mode="w", encoding="utf-8") as tmp:
            tmp_path = Path(tmp.name)
            json.dump(self._data, tmp, indent=2)
        tmp_path.replace(self.path)

    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    @property
    def setup_needed(self) -> bool:
        return not self._data.get("password_hash")

    # ----------------------------------------------------------- password
    def set_password(self, password: str, current: str | None = None) -> None:
        with self._lock:
            if not self.setup_needed and not (current and check_password_hash(self._data["password_hash"], current)):
                raise AuthError("Current password is wrong.")
            if len(password or "") < MIN_PASSWORD_LENGTH:
                raise AuthError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
            self._data["password_hash"] = generate_password_hash(password)
            self._data["password_set_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save()

    def locked_for(self, client: str, now: float | None = None) -> float:
        """Seconds this client must wait before trying again (0 if not locked)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < self.lockout_seconds]
            self._failures[client] = recent
            if len(recent) >= self.max_failures:
                return max(0.0, self.lockout_seconds - (now - recent[0]))
            return 0.0

    def check(self, password: str, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.setup_needed or self.locked_for(client, now) > 0:
            return False
        ok = check_password_hash(self._data["password_hash"], password or "")
        with self._lock:
            if ok:
                self._failures.pop(client, None)
            else:
                self._failures.setdefault(client, []).append(now)
        return ok
