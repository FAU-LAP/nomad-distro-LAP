"""Calls to CAMELS computers that are connected through the CAMELS broker.

CAMELS on a lab computer keeps an outbound connection to the broker. OASIS
sends named operations through the broker's internal API; there is no generic
forwarding. ``call`` accepts the same ``(method, path)`` pairs that are used
for a direct CAMELS API connection, so the dashboard code works for both.

Used by the OASIS app and by the Temporal activities.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException

# A connection whose base URL starts with this goes through the broker.
SCHEME = "camels-remote://"
# Prefix of connection ids of remote computers in the protocol catalog.
CONNECTION_PREFIX = "remote:"

# (HTTP method, CAMELS path) -> broker operation. Path groups become payload
# fields. ``query`` maps a query parameter to a payload field, ``body`` copies
# fields of the JSON body.
ROUTES: list[tuple[str, re.Pattern, str, dict[str, str], tuple[str, ...]]] = [
    ("GET", re.compile(r"^/api/v1/info$"), "get_service_info", {}, ()),
    ("GET", re.compile(r"^/api/v1/health$"), "get_service_info", {}, ()),
    ("GET", re.compile(r"^/api/v1/protocols$"), "list_protocols", {}, ()),
    ("GET", re.compile(r"^/api/v1/protocols/results/(?P<run_id>[^/]+)$"), "get_run_status", {}, ()),
    ("GET", re.compile(r"^/api/v1/protocols/(?P<protocol>[^/]+)/schema$"), "get_protocol_schema", {}, ()),
    ("GET", re.compile(r"^/api/v1/protocols/(?P<protocol>[^/]+)/JSON$"), "get_protocol_definition", {}, ()),
    (
        "POST",
        re.compile(r"^/api/v1/protocols/(?P<protocol>[^/]+)/parameters$"),
        "save_protocol_parameters",
        {},
        ("variables",),
    ),
    (
        "POST",
        re.compile(r"^/api/v1/actions/run/protocols/(?P<protocol>[^/]+)$"),
        "start_measurement",
        {"protocol_uuid": "run_id"},
        ("variables",),
    ),
    ("POST", re.compile(r"^/api/v1/actions/stop$"), "stop_measurement", {}, ()),
    ("GET", re.compile(r"^/api/v1/gui/measurement-status$"), "get_measurement_status", {}, ()),
    ("GET", re.compile(r"^/api/v1/gui/plots$"), "get_plot_count", {}, ()),
    ("GET", re.compile(r"^/api/v1/gui/plot-caches$"), "get_plot_data", {"cursor": "cursor"}, ()),
    ("POST", re.compile(r"^/api/v1/gui/plots/close$"), "close_plots", {}, ()),
    ("POST", re.compile(r"^/api/v1/gui/plots/(?P<plot_id>[^/]+)/close$"), "close_plot", {}, ()),
    ("GET", re.compile(r"^/api/v1/gui/live$"), "get_live_update", {"cursor": "cursor"}, ()),
    ("GET", re.compile(r"^/api/v1/gui/pid-plots$"), "get_pid_plots", {"protocol_name": "protocol"}, ()),
]

# Rejection codes of the agent -> HTTP status for the dashboard.
ERROR_STATUS = {
    "not_found": 404,
    "invalid_parameters": 400,
    "busy": 409,
    "paused": 423,
    "unknown_operation": 501,
    "device_offline": 503,
    "internal": 502,
}

CATALOG_TTL_SECONDS = 5
_catalog_cache: dict[str, tuple[float, list[tuple[str, list[str]]]]] = {}
_catalog_lock = threading.Lock()


def connection(device_id: str, user_id: str) -> tuple[str, str]:
    """Connection tuple for ``request_camels``.

    The second element, the API key of a direct connection, carries the
    requesting user here; it is logged by CAMELS and the broker.
    """
    return SCHEME + device_id, user_id


def device_id_of(base_url: str) -> str | None:
    return base_url[len(SCHEME):] if base_url.startswith(SCHEME) else None


def broker_request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    url = os.environ.get("CAMELS_BROKER_URL", "http://camels_broker:8000").rstrip("/")
    secret = os.environ.get("CAMELS_BROKER_SECRET", "")
    if not secret:
        raise HTTPException(
            503, "Remote computers are not configured on this OASIS: CAMELS_BROKER_SECRET is missing."
        )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"X-Broker-Secret": secret}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = UrlRequest(f"{url}/internal{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(404, "Remote computer not found.") from exc
        raise HTTPException(502, f"The CAMELS broker answered HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise HTTPException(503, "The CAMELS broker is not running.") from exc


def wait_changes(device_id: str, after: int | None, timeout: float) -> dict[str, Any]:
    """Wait until the computer reports a change (see the broker's /changes).

    Returns ``{"seq", "online", "push"}``.
    """
    query = f"timeout={timeout:g}" + ("" if after is None else f"&after={int(after)}")
    return broker_request(
        "GET", f"/devices/{quote(device_id, safe='')}/changes?{query}", timeout=timeout + 10
    )


def owned_device(user_id: str, device_id: str) -> dict[str, Any]:
    state = broker_request("GET", f"/devices/{quote(device_id, safe='')}/state")
    # Devices of other users are reported as missing, not as forbidden.
    if state.get("owner") != user_id:
        raise HTTPException(404, "Remote computer not found.")
    return state


def translate(method: str, path: str, payload: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Map a CAMELS API request to a broker operation and its payload."""
    parts = urlsplit(path)
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    for route_method, pattern, operation, query_fields, body_fields in ROUTES:
        match = pattern.match(parts.path)
        if route_method != method.upper() or not match:
            continue
        command = {name: unquote(value) for name, value in match.groupdict().items()}
        for query_name, field in query_fields.items():
            if query_name in query:
                command[field] = query[query_name]
        for field in body_fields:
            if payload and field in payload:
                command[field] = payload[field]
        return operation, command
    raise HTTPException(501, "This function is not available for remote CAMELS computers.")


def command(
    device_id: str,
    operation: str,
    payload: dict[str, Any] | None = None,
    requested_by: str | None = None,
    timeout: float = 30,
) -> Any:
    """Run one operation on a remote CAMELS computer and return its result."""
    reply = broker_request(
        "POST",
        f"/devices/{quote(device_id, safe='')}/commands",
        {
            "operation": operation,
            "payload": payload or {},
            "requested_by": requested_by,
            "timeout_seconds": timeout,
        },
        timeout=timeout + 10,
    )
    status = reply.get("status")
    if status == "completed":
        return reply.get("result")
    error = reply.get("error") or {}
    code = error.get("code", "internal")
    message = error.get("message") or "The operation failed in CAMELS."
    if status == "unknown":
        raise HTTPException(
            504,
            "No answer from the lab computer in time. The command may still have been carried out.",
        )
    if code == "device_offline":
        message = "The lab computer is offline. Check that CAMELS is running there."
    elif code == "unknown_operation":
        message = "The CAMELS version on the lab computer does not support this. Update CAMELS there."
    raise HTTPException(ERROR_STATUS.get(code, 502), message)


def call(device_id: str, method: str, path: str, payload: dict[str, Any] | None, requested_by: str | None) -> Any:
    """``request_camels`` for a remote computer."""
    operation, command_payload = translate(method, path, payload)
    return command(device_id, operation, command_payload, requested_by)


def device_catalog(user_id: str) -> list[dict[str, Any]]:
    """The user's remote computers: ``device_id``, ``name``, ``online`` and the
    exposed ``protocols`` (empty when offline).

    Cached for a few seconds, because the dashboard asks for every request.
    Broker problems give an empty list, so direct API connections still work.
    """
    now = time.monotonic()
    with _catalog_lock:
        cached = _catalog_cache.get(user_id)
        if cached and now - cached[0] < CATALOG_TTL_SECONDS:
            return cached[1]
    catalog: list[dict[str, Any]] = []
    try:
        devices = broker_request("GET", f"/devices?owner={quote(user_id, safe='')}").get("devices", [])
    except HTTPException:
        devices = []
    for device in devices:
        entry = {
            "device_id": device["device_id"],
            "name": device.get("name") or device["device_id"],
            "online": bool(device.get("online")),
            "protocols": [],
        }
        if entry["online"]:
            try:
                result = command(device["device_id"], "list_protocols", requested_by=user_id, timeout=10)
                entry["protocols"] = [
                    name for name in (result or {}).get("Protocols", []) if isinstance(name, str) and name
                ]
            except HTTPException:
                entry["online"] = False
        catalog.append(entry)
    with _catalog_lock:
        _catalog_cache[user_id] = (now, catalog)
    return catalog


def protocol_catalog(user_id: str) -> list[tuple[str, list[str]]]:
    """``[(device_id, exposed protocol names)]`` of the user's online computers."""
    return [(entry["device_id"], entry["protocols"]) for entry in device_catalog(user_id) if entry["online"]]


def device_for_protocol(user_id: str, protocol_name: str, device_id: str | None = None) -> str | None:
    """The computer that exposes ``protocol_name``; only ``device_id`` if given."""
    for entry in device_catalog(user_id):
        if device_id is not None and entry["device_id"] != device_id:
            continue
        if entry["online"] and protocol_name in entry["protocols"]:
            return entry["device_id"]
    return None
