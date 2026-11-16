"""Command-line access to the internal broker API.

Until the OASIS setup page uses the broker, this replaces it for testing. Run it
inside the broker container, where the internal API and the secret are available:

    docker compose exec camels_broker python -m camels_broker.admin create-device "Lab PC 1"
    docker compose exec camels_broker python -m camels_broker.admin list
    docker compose exec camels_broker python -m camels_broker.admin command dev_... list_protocols
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m camels_broker.admin")
    parser.add_argument("--url", default=os.environ.get("CAMELS_BROKER_URL", "http://127.0.0.1:8000"))
    sub = parser.add_subparsers(dest="action", required=True)

    create = sub.add_parser("create-device", help="create a device and print its pairing code")
    create.add_argument("name")
    create.add_argument("--owner")
    sub.add_parser("list", help="list devices and their connection state")
    code = sub.add_parser("pairing-code", help="issue a new pairing code for a device")
    code.add_argument("device_id")
    delete = sub.add_parser("delete", help="delete a device and revoke its token")
    delete.add_argument("device_id")
    state = sub.add_parser("state", help="show the state of one device")
    state.add_argument("device_id")
    command = sub.add_parser("command", help="send an operation to a device")
    command.add_argument("device_id")
    command.add_argument("operation")
    command.add_argument("--payload", default="{}", help="JSON object")
    command.add_argument("--timeout", type=float)
    args = parser.parse_args(argv)

    secret = os.environ.get("CAMELS_BROKER_SECRET")
    if not secret:
        print("CAMELS_BROKER_SECRET is not set", file=sys.stderr)
        return 2
    client = httpx.Client(base_url=args.url.rstrip("/") + "/internal", headers={"X-Broker-Secret": secret}, timeout=130)

    if args.action == "create-device":
        response = client.post("/devices", json={"name": args.name, "owner": args.owner})
    elif args.action == "list":
        response = client.get("/devices")
    elif args.action == "pairing-code":
        response = client.post(f"/devices/{args.device_id}/pairing-code")
    elif args.action == "delete":
        response = client.delete(f"/devices/{args.device_id}")
    elif args.action == "state":
        response = client.get(f"/devices/{args.device_id}/state")
    else:
        body = {"operation": args.operation, "payload": json.loads(args.payload), "requested_by": "admin-cli"}
        if args.timeout:
            body["timeout_seconds"] = args.timeout
        response = client.post(f"/devices/{args.device_id}/commands", json=body)

    data = response.json() if response.content else {}
    if response.is_error:
        print(f"HTTP {response.status_code}: {data}", file=sys.stderr)
        return 1
    if "pairing_code" in data:
        expires = datetime.fromtimestamp(data["pairing_code_expires_at"], timezone.utc).strftime("%H:%M:%S UTC")
        print(f"Device:       {data['device_id']}")
        print(f"Pairing code: {data['pairing_code']}  (valid once, until {expires})")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
