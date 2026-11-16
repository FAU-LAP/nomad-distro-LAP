"""Broker between OASIS and CAMELS agents.

CAMELS agents open an outbound WebSocket to this service. OASIS components
(plugin API, Temporal activities) reach the agents through the internal HTTP
API, which is not routed by Nginx and requires the broker secret.

See docs/camels_remote_protocol.md for the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .store import DeviceStore, PairingError

log = logging.getLogger("camels_broker")

SUBPROTOCOL = "camels-remote.v1"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
HELLO_TIMEOUT_SECONDS = 10

CLOSE_REVOKED = 4401
CLOSE_REPLACED = 4409
CLOSE_UNSUPPORTED_VERSION = 4426
CLOSE_PROTOCOL_ERROR = 1008


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def envelope(type_: str, **fields) -> dict:
    return {"version": PROTOCOL_VERSION, "type": type_, "id": uuid.uuid4().hex, "ts": utc_now(), **fields}


@dataclass
class Settings:
    secret: str
    db_path: str = "/data/broker.db"
    public_prefix: str = "/nomad-oasis/camels-remote"
    command_timeout_seconds: float = 15.0
    pairing_ttl_seconds: float = 600.0
    pairing_attempts_per_minute: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("CAMELS_BROKER_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("CAMELS_BROKER_SECRET must be set to at least 32 characters")
        return cls(
            secret=secret,
            db_path=os.environ.get("CAMELS_BROKER_DB", cls.db_path),
            public_prefix=os.environ.get("CAMELS_BROKER_PUBLIC_PREFIX", cls.public_prefix).rstrip("/"),
        )


@dataclass
class Connection:
    device_id: str
    websocket: WebSocket
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    connected_at: float = field(default_factory=time.time)
    hello: dict = field(default_factory=dict)
    status: dict | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(message))

    async def close(self, code: int, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            if self.websocket.application_state == WebSocketState.CONNECTED:
                await self.websocket.close(code=code, reason=reason)


class Hub:
    """Active agent connections, one per device."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self.last_status: dict[str, dict] = {}

    def get(self, device_id: str) -> Connection | None:
        conn = self.connections.get(device_id)
        return None if conn is None or conn.closed else conn

    async def register(self, conn: Connection) -> None:
        old = self.connections.get(conn.device_id)
        self.connections[conn.device_id] = conn
        if old is not None and old is not conn:
            log.info("device %s: newer connection replaces session %s", conn.device_id, old.session_id)
            self._fail_pending(old, "device_offline", "The connection was replaced.")
            await old.close(CLOSE_REPLACED, "replaced by a newer connection")

    def unregister(self, conn: Connection) -> None:
        conn.closed = True
        if self.connections.get(conn.device_id) is conn:
            del self.connections[conn.device_id]
        self._fail_pending(conn, "device_offline", "The connection was lost.")

    async def disconnect(self, device_id: str, code: int, reason: str) -> None:
        conn = self.connections.pop(device_id, None)
        if conn is not None:
            self._fail_pending(conn, "device_offline", reason)
            await conn.close(code, reason)

    @staticmethod
    def _fail_pending(conn: Connection, code: str, message: str) -> None:
        for future in conn.pending.values():
            if not future.done():
                future.set_result(
                    {"status": "unknown", "error": {"code": code, "message": message}}
                )
        conn.pending.clear()

    async def send_command(
        self,
        device_id: str,
        operation: str,
        payload: dict,
        requested_by: str | None,
        timeout: float,
    ) -> dict:
        conn = self.get(device_id)
        if conn is None:
            return {
                "status": "rejected",
                "error": {"code": "device_offline", "message": "The device is not connected."},
            }
        command_id = "cmd-" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        conn.pending[command_id] = future
        try:
            await conn.send(
                envelope(
                    "command",
                    command_id=command_id,
                    operation=operation,
                    requested_by=requested_by,
                    payload=payload,
                )
            )
        except Exception:
            conn.pending.pop(command_id, None)
            # The send may have reached the agent before the error, so the
            # outcome is unknown rather than rejected.
            return {
                "status": "unknown",
                "error": {"code": "device_offline", "message": "Sending the command failed."},
            }
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {
                "status": "unknown",
                "error": {"code": "timeout", "message": "The device did not answer in time."},
            }
        finally:
            conn.pending.pop(command_id, None)


class PairRequest(BaseModel):
    pairing_code: str = Field(..., max_length=32)
    hostname: str | None = Field(None, max_length=255)
    camels_version: str | None = Field(None, max_length=64)


class CreateDeviceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    owner: str | None = Field(None, max_length=255)


class CommandRequest(BaseModel):
    operation: str = Field(..., min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    requested_by: str | None = Field(None, max_length=255)
    timeout_seconds: float | None = Field(None, gt=0, le=120)


class AttemptLimiter:
    """Global limit for failed pairing attempts, to slow down code guessing."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.failures: deque[float] = deque()

    def blocked(self) -> bool:
        cutoff = time.monotonic() - 60
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()
        return len(self.failures) >= self.per_minute

    def record_failure(self) -> None:
        self.failures.append(time.monotonic())


def bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = DeviceStore(settings.db_path)
    hub = Hub()
    limiter = AttemptLimiter(settings.pairing_attempts_per_minute)

    app = FastAPI(title="CAMELS remote broker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.hub = hub
    app.state.settings = settings

    # ------------------------------------------------------------------
    # Public endpoints, routed by Nginx
    # ------------------------------------------------------------------
    public = APIRouter(prefix=settings.public_prefix)

    @public.post("/pair")
    async def pair(body: PairRequest, request: Request):
        if limiter.blocked():
            raise HTTPException(429, "Too many pairing attempts. Try again in a minute.")
        try:
            device, token = store.redeem_pairing_code(body.pairing_code, body.hostname)
        except PairingError:
            limiter.record_failure()
            await asyncio.sleep(1)
            raise HTTPException(400, "The pairing code is invalid or expired.")
        # The new token replaces the old one, so an old session must end.
        await hub.disconnect(device.device_id, CLOSE_REVOKED, "device paired again")
        log.info("device %s paired from host %s", device.device_id, body.hostname)
        return {"device_id": device.device_id, "device_name": device.name, "device_token": token}

    @public.websocket("/agent")
    async def agent_socket(websocket: WebSocket):
        token = bearer_token(websocket.headers.get("authorization"))
        device = store.device_for_token(token) if token else None
        if device is None:
            await deny(websocket, 401, "Invalid device token")
            return
        if SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
            await websocket.accept()
            await websocket.close(CLOSE_UNSUPPORTED_VERSION, "unsupported protocol version")
            return
        await websocket.accept(subprotocol=SUBPROTOCOL)
        conn = Connection(device_id=device.device_id, websocket=websocket)
        try:
            hello = await asyncio.wait_for(receive_json(websocket), HELLO_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await conn.close(CLOSE_PROTOCOL_ERROR, "expected hello")
            return
        if hello.get("type") != "hello":
            await conn.close(CLOSE_PROTOCOL_ERROR, "expected hello")
            return
        conn.hello = {
            key: hello.get(key)
            for key in ("camels_version", "hostname", "operations", "measurement")
        }
        if hello.get("measurement"):
            hub.last_status[device.device_id] = hello["measurement"]
        await hub.register(conn)
        store.touch(device.device_id, hello.get("hostname"))
        log.info("device %s connected, session %s", device.device_id, conn.session_id)
        try:
            await conn.send(envelope("welcome", session_id=conn.session_id, plot_subscriptions=[]))
            while True:
                try:
                    message = await receive_json(websocket)
                except ValueError as exc:
                    log.warning("device %s: ignored message: %s", device.device_id, exc)
                    continue
                handle_agent_message(conn, message)
                store.touch(device.device_id)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(conn)
            log.info("device %s disconnected, session %s", device.device_id, conn.session_id)

    def handle_agent_message(conn: Connection, message: dict) -> None:
        kind = message.get("type")
        if kind == "command_result":
            future = conn.pending.get(message.get("command_id"))
            if future is not None and not future.done():
                result = {"status": message.get("status")}
                for key in ("result", "error"):
                    if key in message:
                        result[key] = message[key]
                future.set_result(result)
        elif kind == "status":
            status = {k: v for k, v in message.items() if k not in ("version", "type", "id")}
            conn.status = status
            hub.last_status[conn.device_id] = status
        else:
            log.info("device %s: ignored message type %r", conn.device_id, kind)

    app.include_router(public)

    # ------------------------------------------------------------------
    # Internal endpoints, only reachable on the Docker network
    # ------------------------------------------------------------------
    def require_secret(x_broker_secret: str | None = Header(None)) -> None:
        if not x_broker_secret or not hmac.compare_digest(
            x_broker_secret.encode(), settings.secret.encode()
        ):
            raise HTTPException(401, "Invalid broker secret")

    internal = APIRouter(prefix="/internal", dependencies=[Depends(require_secret)])

    def device_or_404(device_id: str):
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        return device

    def device_state(device) -> dict:
        conn = hub.get(device.device_id)
        return {
            **device.as_dict(),
            "online": conn is not None,
            "session_id": conn.session_id if conn else None,
            "connected_since": conn.connected_at if conn else None,
            "agent": conn.hello if conn else None,
            "measurement": hub.last_status.get(device.device_id),
        }

    @internal.post("/devices")
    async def create_device(body: CreateDeviceRequest):
        device = store.create_device(body.name, body.owner)
        code, expires_at = store.issue_pairing_code(device.device_id, settings.pairing_ttl_seconds)
        return {**device.as_dict(), "pairing_code": code, "pairing_code_expires_at": expires_at}

    @internal.get("/devices")
    async def list_devices(owner: str | None = None):
        return {"devices": [device_state(device) for device in store.list_devices(owner)]}

    @internal.post("/devices/{device_id}/pairing-code")
    async def new_pairing_code(device_id: str):
        device_or_404(device_id)
        code, expires_at = store.issue_pairing_code(device_id, settings.pairing_ttl_seconds)
        return {"device_id": device_id, "pairing_code": code, "pairing_code_expires_at": expires_at}

    @internal.delete("/devices/{device_id}")
    async def delete_device(device_id: str):
        device_or_404(device_id)
        await hub.disconnect(device_id, CLOSE_REVOKED, "device deleted")
        hub.last_status.pop(device_id, None)
        store.delete_device(device_id)
        return {"deleted": device_id}

    @internal.get("/devices/{device_id}/state")
    async def get_state(device_id: str):
        return device_state(device_or_404(device_id))

    @internal.post("/devices/{device_id}/commands")
    async def send_command(device_id: str, body: CommandRequest):
        device_or_404(device_id)
        result = await hub.send_command(
            device_id,
            body.operation,
            body.payload,
            body.requested_by,
            body.timeout_seconds or settings.command_timeout_seconds,
        )
        log.info(
            "command device=%s operation=%s by=%s status=%s",
            device_id,
            body.operation,
            body.requested_by,
            result.get("status"),
        )
        return result

    app.include_router(internal)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "connected_devices": len(hub.connections)}

    return app


async def receive_json(websocket: WebSocket) -> dict:
    text = await websocket.receive_text()
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large")
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("message is not an object")
    return message


async def deny(websocket: WebSocket, status_code: int, detail: str) -> None:
    """Reject the handshake with an HTTP status, so the agent sees 401."""
    try:
        await websocket.send_denial_response(JSONResponse({"detail": detail}, status_code=status_code))
    except RuntimeError:
        # The server does not support denial responses; this returns HTTP 403.
        await websocket.close(CLOSE_PROTOCOL_ERROR)


def generate_secret() -> str:
    return secrets.token_urlsafe(48)
