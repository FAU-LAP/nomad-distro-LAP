"""SQLite storage for paired CAMELS devices.

Only hashes of device tokens and pairing codes are stored. Both secrets are
random with at least 40 bits (codes) or 256 bits (tokens), so a plain SHA-256
hash is sufficient and allows a direct lookup.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_pairing_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


@dataclass(frozen=True)
class Device:
    device_id: str
    name: str
    owner: str | None
    paired: bool
    hostname: str | None
    created_at: float
    paired_at: float | None
    last_seen: float | None

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "owner": self.owner,
            "paired": self.paired,
            "hostname": self.hostname,
            "created_at": self.created_at,
            "paired_at": self.paired_at,
            "last_seen": self.last_seen,
        }


class PairingError(Exception):
    """The pairing code is unknown, used or expired."""


class DeviceStore:
    def __init__(self, path: str | Path):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id  TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    owner      TEXT,
                    token_hash TEXT UNIQUE,
                    hostname   TEXT,
                    created_at REAL NOT NULL,
                    paired_at  REAL,
                    last_seen  REAL
                );
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash  TEXT PRIMARY KEY,
                    device_id  TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                """
            )

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _device(row: sqlite3.Row) -> Device:
        return Device(
            device_id=row["device_id"],
            name=row["name"],
            owner=row["owner"],
            paired=row["token_hash"] is not None,
            hostname=row["hostname"],
            created_at=row["created_at"],
            paired_at=row["paired_at"],
            last_seen=row["last_seen"],
        )

    def create_device(self, name: str, owner: str | None) -> Device:
        device_id = "dev_" + secrets.token_hex(6)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO devices (device_id, name, owner, created_at) VALUES (?, ?, ?, ?)",
                (device_id, name, owner, now),
            )
        return self.get_device(device_id)

    def get_device(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._device(row) if row else None

    def list_devices(self, owner: str | None = None) -> list[Device]:
        with self._lock:
            if owner is None:
                rows = self._conn.execute(
                    "SELECT * FROM devices ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM devices WHERE owner = ? ORDER BY created_at", (owner,)
                ).fetchall()
        return [self._device(row) for row in rows]

    def delete_device(self, device_id: str) -> bool:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
            cursor = self._conn.execute(
                "DELETE FROM devices WHERE device_id = ?", (device_id,)
            )
        return cursor.rowcount > 0

    def issue_pairing_code(self, device_id: str, ttl_seconds: float) -> tuple[str, float]:
        """Create a single-use code. Older codes of the device stop working."""
        code = new_pairing_code()
        expires_at = time.time() + ttl_seconds
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
            self._conn.execute(
                "INSERT INTO pairing_codes (code_hash, device_id, expires_at) VALUES (?, ?, ?)",
                (_hash(normalize_pairing_code(code)), device_id, expires_at),
            )
        return code, expires_at

    def redeem_pairing_code(self, code: str, hostname: str | None) -> tuple[Device, str]:
        """Consume a code and return the device with a new token.

        The new token replaces any earlier token of the device.
        """
        code_hash = _hash(normalize_pairing_code(code))
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT device_id, expires_at FROM pairing_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if row is None:
                raise PairingError("invalid pairing code")
            self._conn.execute("DELETE FROM pairing_codes WHERE code_hash = ?", (code_hash,))
            if row["expires_at"] < now:
                raise PairingError("expired pairing code")
            self._conn.execute(
                "UPDATE devices SET token_hash = ?, hostname = ?, paired_at = ? WHERE device_id = ?",
                (_hash(token), hostname, now, row["device_id"]),
            )
        return self.get_device(row["device_id"]), token

    def device_for_token(self, token: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
        return self._device(row) if row else None

    def touch(self, device_id: str, hostname: str | None = None) -> None:
        with self._lock, self._conn:
            if hostname:
                self._conn.execute(
                    "UPDATE devices SET last_seen = ?, hostname = ? WHERE device_id = ?",
                    (time.time(), hostname, device_id),
                )
            else:
                self._conn.execute(
                    "UPDATE devices SET last_seen = ? WHERE device_id = ?",
                    (time.time(), device_id),
                )
