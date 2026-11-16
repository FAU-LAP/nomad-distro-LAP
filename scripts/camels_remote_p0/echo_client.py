"""Check from the lab computer that an outbound WSS channel to OASIS works.

Exit code 0 means that every check passed.

The token is read from the P0_TOKEN environment variable so that it does not
appear in the process list or shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidMessage,
    InvalidProxy,
    InvalidStatus,
)

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
PAYLOAD_KIB = (1, 64, 256, 1024)
REPLY_TIMEOUT_SECONDS = 15
MAX_BACKOFF_SECONDS = 60

STATUS_HINTS = {
    401: "the proxy did not forward the Authorization header, or the token is wrong",
    404: "wrong URL path, or the proxy has no route for it",
    407: "a network proxy requires authentication",
    426: "the proxy removed the WebSocket Upgrade headers",
    502: "the proxy cannot reach the echo server",
    503: "the proxy or echo server is unavailable",
    504: "the proxy timed out while reaching the echo server",
}


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append((name, passed, detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(passed for _, passed, _ in self.checks)


def explain_connect_error(error: BaseException) -> str:
    """Name the network layer on which the connection failed."""
    if isinstance(error, socket.gaierror):
        return f"DNS: the host name could not be resolved ({error})"
    if isinstance(error, ssl.SSLCertVerificationError):
        return f"TLS: the certificate is not trusted, use --ca-file for a private CA ({error.verify_message})"
    if isinstance(error, ssl.SSLError):
        return f"TLS: the handshake failed ({error})"
    if isinstance(error, InvalidStatus):
        status = int(error.response.status_code)
        hint = STATUS_HINTS.get(status, "unexpected response")
        return f"HTTP {status}: {hint}"
    if isinstance(error, InvalidProxy):
        return f"Proxy: the configured proxy cannot be used ({error})"
    if isinstance(error, (InvalidHandshake, InvalidMessage)):
        return f"WebSocket: the upgrade failed, possibly because of a network proxy ({error})"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "TCP: timed out, a firewall or the network probably drops the connection"
    if isinstance(error, ConnectionRefusedError):
        return "TCP: refused, the host is reachable but nothing listens on this port"
    if isinstance(error, OSError):
        return f"TCP: {error}"
    return f"{type(error).__name__}: {error}"


class Probe:
    def __init__(self, args: argparse.Namespace, token: str):
        self.url = args.url
        self.token = token
        self.tls = None
        if args.url.startswith("wss://"):
            self.tls = ssl.create_default_context(cafile=args.ca_file)

    def open(self):
        # Pings stay off: the idle checks must not send any traffic, and the
        # soak check sends its own heartbeat. Compression stays off so that
        # the payload sizes are what actually crosses the network.
        return connect(
            self.url,
            ssl=self.tls,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            ping_interval=None,
            compression=None,
            max_size=MAX_MESSAGE_BYTES,
            open_timeout=REPLY_TIMEOUT_SECONDS,
            close_timeout=2,
        )

    @staticmethod
    async def echo(websocket: ClientConnection, data: str = "") -> float:
        """Return the round-trip time in milliseconds."""
        message_id = uuid.uuid4().hex
        start = time.perf_counter()
        await websocket.send(json.dumps({"op": "echo", "id": message_id, "data": data}))
        raw = await asyncio.wait_for(websocket.recv(), REPLY_TIMEOUT_SECONDS)
        reply = json.loads(raw)
        if reply.get("id") != message_id or reply.get("data") != data:
            raise ValueError("the echoed message differs from the message sent")
        return (time.perf_counter() - start) * 1000

    @staticmethod
    async def wait_for_push(websocket: ClientConnection, delay: float) -> None:
        """Stay silent until the server sends a message on its own."""
        message_id = uuid.uuid4().hex
        await websocket.send(json.dumps({"op": "push_after", "id": message_id, "delay": delay}))
        raw = await asyncio.wait_for(websocket.recv(), delay + REPLY_TIMEOUT_SECONDS)
        if json.loads(raw).get("id") != message_id:
            raise ValueError("received an unexpected push message")


async def check_handshake(probe: Probe, report: Report) -> bool:
    try:
        async with probe.open() as websocket:
            latencies = [await probe.echo(websocket) for _ in range(5)]
    except (OSError, ConnectionClosed, InvalidHandshake, InvalidMessage,
            InvalidProxy, ValueError, asyncio.TimeoutError) as error:
        report.add("handshake", False, explain_connect_error(error))
        return False
    report.add("handshake", True, f"5 echoes, longest round trip {max(latencies):.1f} ms")
    return True


async def check_payloads(probe: Probe, report: Report) -> None:
    for size_kib in PAYLOAD_KIB:
        name = f"payload {size_kib} KiB"
        data = random.randbytes(size_kib * 512).hex()
        try:
            async with probe.open() as websocket:
                latency = await probe.echo(websocket, data)
            report.add(name, True, f"round trip {latency:.1f} ms")
        except (OSError, ConnectionClosed, InvalidHandshake, ValueError,
                asyncio.TimeoutError) as error:
            report.add(name, False, explain_connect_error(error))


async def check_idle(probe: Probe, report: Report, delays: list[float]) -> None:
    """Simulate a command that OASIS sends after the channel was silent."""
    for delay in delays:
        name = f"server message after {delay:g} s of silence"
        print(f"Waiting: {name} ...", flush=True)
        try:
            async with probe.open() as websocket:
                await probe.wait_for_push(websocket, delay)
            report.add(name, True, "received")
        except (OSError, ConnectionClosed, InvalidHandshake, ValueError,
                asyncio.TimeoutError) as error:
            report.add(name, False, f"the path probably closes idle connections: {explain_connect_error(error)}")


async def check_soak(probe: Probe, report: Report, minutes: float, heartbeat: float) -> None:
    """Hold one connection open and reconnect with backoff if it drops."""
    if minutes <= 0:
        return
    print(f"Holding the connection for {minutes:g} min ...", flush=True)
    end = time.monotonic() + minutes * 60
    latencies: list[float] = []
    disconnects = 0
    backoff = 1.0
    try:
        while time.monotonic() < end:
            try:
                async with probe.open() as websocket:
                    backoff = 1.0
                    while time.monotonic() < end:
                        latencies.append(await probe.echo(websocket))
                        await asyncio.sleep(min(heartbeat, max(end - time.monotonic(), 0)))
            except (OSError, ConnectionClosed, InvalidHandshake, ValueError,
                    asyncio.TimeoutError) as error:
                disconnects += 1
                print(f"Disconnected: {explain_connect_error(error)}; retry in {backoff:.0f} s", flush=True)
                await asyncio.sleep(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    finally:
        longest = max(latencies, default=0.0)
        report.add(
            f"soak {minutes:g} min",
            disconnects == 0 and bool(latencies),
            f"{len(latencies)} heartbeats, {disconnects} connection errors, longest round trip {longest:.1f} ms",
        )


async def run(args: argparse.Namespace, token: str, report: Report) -> None:
    probe = Probe(args, token)
    if not await check_handshake(probe, report):
        return
    await check_payloads(probe, report)
    await check_idle(probe, report, args.idle_seconds)
    await check_soak(probe, report, args.soak_minutes, args.heartbeat)


def parse_delays(text: str) -> list[float]:
    return [float(value) for value in text.split(",") if value.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="For example wss://oasis.example.org/camels-remote/p0")
    parser.add_argument("--ca-file", help="CA or self-signed certificate to trust instead of the system certificates")
    parser.add_argument("--idle-seconds", type=parse_delays, default=[30.0, 60.0, 120.0],
                        help="Comma-separated silent periods before a server message")
    parser.add_argument("--soak-minutes", type=float, default=30)
    parser.add_argument("--heartbeat", type=float, default=20)
    args = parser.parse_args()

    if not args.url.startswith(("ws://", "wss://")):
        parser.error("The URL must start with wss:// or ws://.")
    if args.ca_file and not args.url.startswith("wss://"):
        parser.error("--ca-file requires a wss:// URL.")
    token = os.environ.get("P0_TOKEN")
    if not token:
        parser.error("Set the P0_TOKEN environment variable.")

    if args.url.startswith("ws://"):
        print("WARNING: ws:// is not encrypted and only proves local behaviour, not the real path.")
    proxies = {key: value for key, value in urllib.request.getproxies().items() if key in ("http", "https")}
    print(f"Target: {args.url}\nSystem proxy: {proxies or 'none'}", flush=True)

    report = Report()
    try:
        asyncio.run(run(args, token, report))
    except KeyboardInterrupt:
        report.add("run", False, "interrupted before all checks finished")

    print(f"\nRESULT: {'PASS' if report.passed else 'FAIL'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
