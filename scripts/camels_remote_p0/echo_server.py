"""WebSocket echo server for the CAMELS remote-control P0 network check.

This is a throwaway test tool, not the CAMELS broker. It only echoes messages
and sends delayed messages back to the client that requested them.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import http
import json
import logging
import os
import ssl

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

PATH = "/camels-remote/p0"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_PUSH_DELAY_SECONDS = 3600

log = logging.getLogger("p0-echo")


async def push_after(websocket: ServerConnection, message_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await websocket.send(json.dumps({"op": "push", "id": message_id}))
    except ConnectionClosed:
        log.info("client disconnected before push %s", message_id)


async def handle(websocket: ServerConnection) -> None:
    peer = websocket.remote_address
    log.info("connected: %s", peer)
    pushes: set[asyncio.Task] = set()
    try:
        async for raw in websocket:
            message = json.loads(raw)
            operation = message.get("op")
            if operation == "echo":
                await websocket.send(raw)
            elif operation == "push_after":
                delay = min(float(message["delay"]), MAX_PUSH_DELAY_SECONDS)
                task = asyncio.create_task(push_after(websocket, message["id"], delay))
                pushes.add(task)
                task.add_done_callback(pushes.discard)
    except ConnectionClosed:
        pass
    except (KeyError, TypeError, ValueError) as error:
        log.warning("invalid message from %s: %s", peer, error)
        await websocket.close(1003, "invalid message")
    finally:
        for task in pushes:
            task.cancel()
        log.info("disconnected: %s", peer)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tls-cert", help="Certificate, when no reverse proxy terminates TLS")
    parser.add_argument("--tls-key", help="Private key for --tls-cert")
    args = parser.parse_args()

    token = os.environ.get("P0_TOKEN")
    if not token:
        parser.error("Set the P0_TOKEN environment variable.")

    def check_request(connection: ServerConnection, request):
        if request.path != PATH:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not found\n")
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Invalid token\n")
        return None

    tls_context = None
    if args.tls_cert:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(args.tls_cert, args.tls_key)

    # Server pings stay off so that the idle checks are really silent.
    async with serve(
        handle,
        args.host,
        args.port,
        process_request=check_request,
        ping_interval=None,
        max_size=MAX_MESSAGE_BYTES,
        ssl=tls_context,
    ):
        log.info("listening on %s:%s%s", args.host, args.port, PATH)
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
