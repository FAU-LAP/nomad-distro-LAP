# CAMELS remote-control protocol, version 1 (draft)

This document defines how a CAMELS agent and the OASIS broker communicate. CAMELS
always opens the connection, so the lab computer needs no inbound port.

Status: draft. Phase P1 is implemented: pairing, the connection, and the read-only
operations (`camels_broker/` in this repository, `nomad_camels/api/remote_agent.py`
in CAMELS). Live plots use option B: CAMELS sends data and OASIS renders it. No
generic HTTP or Dash proxy crosses this channel.

## 1. Components

```text
Browser ── HTTPS ──> OASIS plugin API ──┐
                                        ├─ internal HTTP ─> camels-broker <═ WSS ═> CAMELS agent
Temporal activity (cpu_worker) ─────────┘                                               │
                                                     in-process ASGI, no port ──> CAMELS FastAPI routes
```

- **Broker:** a separate container holding device connections. The plugin API and
  Temporal activities run in other processes, so they reach it over internal HTTP.
- **Agent:** runs inside the CAMELS process. It maps whitelisted operations to
  existing FastAPI routes using `httpx.ASGITransport`, which reuses their API-key,
  exposure and variable checks without opening a port.

## 2. Device pairing

1. A signed-in user creates a device in the OASIS setup page and receives a
   pairing code. The code is valid once, for 10 minutes.
2. The user enters the OASIS URL and the code in the CAMELS settings.
3. CAMELS calls the following endpoint over HTTPS:

   ```http
   POST <api_base_path>/camels-remote/pair
   {"pairing_code": "...", "hostname": "lab-pc-01", "camels_version": "..."}
   ```

   ```json
   {"device_id": "dev_01", "device_token": "..."}
   ```

4. CAMELS stores the token with Windows DPAPI. OASIS stores only its hash.

A device token identifies a computer. It never authorizes a measurement by itself:
OASIS authorizes each user request against the device's owner and shared users.

Remote mode is off by default and is enabled in the CAMELS settings. Deleting the
device in OASIS revokes the token.

## 3. Connection

```http
GET wss://<oasis-host><api_base_path>/camels-remote/agent
Authorization: Bearer <device_token>
Sec-WebSocket-Protocol: camels-remote.v1
```

- TLS certificate verification is mandatory. A private CA is configured explicitly.
- The token is sent only as a header, never in the URL.
- A rejected credential returns HTTP 401 during the handshake. The agent stops
  retrying and asks the user to pair again.
- A device has one active connection. A newer connection replaces the older one.

### Liveness and reconnection

- Both sides send WebSocket ping frames every 20 seconds. No pong within 20 seconds
  closes the connection. Proxy read timeouts must exceed 60 seconds.
- The agent reconnects after 1, 2, 4 ... seconds up to 60 seconds, with ±20% jitter.
- The broker marks a device offline as soon as its connection is lost.

### Close codes

| Code | Meaning | Agent behaviour |
|---|---|---|
| `4401` | Credential revoked during the session | Stop, ask to pair again |
| `4409` | Replaced by a newer connection | Stop, warn the user |
| `4426` | Unsupported protocol version | Stop, ask to update |
| Other | Network or server problem | Reconnect |

## 4. Message format

Every message is a UTF-8 JSON text frame of at most 1 MiB.

```json
{"version": 1, "type": "<type>", "id": "<unique message id>", "ts": "2026-11-06T10:00:00Z"}
```

The examples below omit these common fields. The broker takes the device identity
only from the authenticated connection, so messages carry no `device_id`.

Receivers ignore unknown fields. An unknown `type` is logged and ignored.

| Direction | Type | Purpose |
|---|---|---|
| Agent → Broker | `hello` | Report identity and current state after connecting |
| Broker → Agent | `welcome` | Accept the session |
| Broker → Agent | `command` | Request an operation |
| Agent → Broker | `command_result` | Answer one command |
| Agent → Broker | `status` | Report measurement status |
| Broker → Agent | `plot_subscribe` | Start sending plot data |
| Broker → Agent | `plot_unsubscribe` | Stop sending plot data |
| Agent → Broker | `plot_snapshot` | Send plot data |

### `hello` and `welcome`

The first message after connecting. The state lets the broker recover after a
reconnection, including a measurement that continued while the agent was offline.

```json
{"type": "hello", "camels_version": "1.9.0", "hostname": "lab-pc-01",
 "operations": ["list_protocols", "start_protocol"],
 "measurement": {"state": "running", "run_id": "0d3f...", "protocol_name": "IV_Sweep"}}
```

```json
{"type": "welcome", "session_id": "...", "plot_subscriptions": []}
```

### `command` and `command_result`

```json
{"type": "command", "command_id": "cmd-4821", "operation": "start_protocol",
 "requested_by": "<NOMAD user id>",
 "payload": {"protocol": "IV_Sweep", "run_id": "0d3f...", "variables": {"voltage_start": -1.0}}}
```

```json
{"type": "command_result", "command_id": "cmd-4821", "status": "accepted",
 "result": {"run_id": "0d3f..."}}
```

```json
{"type": "command_result", "command_id": "cmd-4821", "status": "rejected",
 "error": {"code": "busy", "message": "Another measurement is running."}}
```

`status` is one of:

- `completed`: the operation finished, and `result` holds its response.
- `accepted`: the operation started, and `status` messages report its progress.
  Accepted does not mean that the measurement finished.
- `rejected`: the operation was not carried out, and `error` gives the reason.
- `unknown`: only created by the broker. The command was sent, but no answer
  arrived before the deadline or the connection was lost. The operation may have
  run. `error.code` is `timeout` or `device_offline`.

The agent answers an unknown `operation` with `rejected`.

## 5. Operations

This whitelist is complete: the agent has no generic forwarding operation. The
agent lists the operations it supports in `hello`. P1 supports the read-only
operations; `save_parameters`, `start_protocol`, `stop_protocol`, `clear_plot` and
`close_plots` follow with their safeguards.
`{protocol}` is URL-encoded. Paths start with `/api/v1`.

| Operation | Current CAMELS route | Result |
|---|---|---|
| `get_service_info` | `GET /info` | `completed` |
| `list_protocols` | `GET /protocols` | `completed` |
| `get_protocol_schema` | `GET /protocols/{protocol}/schema` | `completed` |
| `get_protocol_definition` | `GET /protocols/{protocol}/JSON` | `completed` |
| `save_parameters` | `POST /protocols/{protocol}/parameters` | `completed` |
| `start_protocol` | `POST /actions/run/protocols/{protocol}?protocol_uuid={run_id}` | `accepted` |
| `stop_protocol` | New route required | `accepted` |
| `get_run_status` | `GET /protocols/results/{run_id}` | `completed` |
| `get_measurement_status` | `GET /gui/measurement-status` | `completed` |
| `clear_plot` | `POST /gui/browser-plots/{protocol}/{port}/clear_data` | `completed` |
| `close_plots` | `POST /gui/plots/close` | `completed` |

Error codes:

| Code | Source |
|---|---|
| `device_offline` | Broker: no active connection (`rejected`), or it was lost while waiting (`unknown`) |
| `timeout` | Broker: no answer before the deadline (`unknown`) |
| `unknown_operation` | Agent: not in the whitelist |
| `not_found` | Route returned 404, including protocols that are not exposed |
| `invalid_parameters` | Route returned 400 or 422 |
| `busy` | Route returned 409: a measurement is running |
| `internal` | Other failures. The message contains no secrets or stack traces |

## 6. Delivery rules

These rules prevent a retry from starting a second measurement.

1. **OASIS creates `run_id`.** The value is a UUID created before the first attempt
   and reused for every retry of that start. For Temporal, this means generating it
   in the workflow and passing it to the activity.
2. **CAMELS deduplicates `run_id`.** A known `run_id` returns its existing state and
   never starts another measurement.
3. **The broker does not queue commands.** An offline device gets `device_offline`
   immediately, and a reconnection does not replay anything.
4. **The broker enforces the deadline.** Each internal request has a deadline, by
   default 15 seconds. The broker's clock decides, so that a wrong lab clock
   cannot reject valid commands.
5. **A lost answer stays unknown.** If the connection drops after a start was sent,
   OASIS treats the result as unknown and checks `run_id` after reconnection.

## 7. Measurement status

The agent sends `status` when the state changes, and every 2 seconds during a
measurement.

```json
{"type": "status", "state": "running", "run_id": "0d3f...", "protocol_name": "IV_Sweep",
 "progress": 42.0, "elapsed_seconds": 12.5, "remaining_seconds": 17.0}
```

`state` is `idle`, `preparing`, `running`, `completed`, `failed` or `aborted`. The
last three are final. A final message also has `result_file` or `error`.

The agent translates the CAMELS values into these states, so OASIS no longer needs
to treat a status containing a path separator as success.

**Disconnection:** a running measurement continues. Its safety limits and interlocks
must work locally, without OASIS. After reconnection, `hello` reports the current
state; if the run ended while offline, the agent also sends its final `status`.

## 8. Live plots

The broker subscribes only while a browser is viewing the measurement. A subscription
applies to the current connection, so the broker sends it again after reconnection.

```json
{"type": "plot_subscribe", "run_id": "0d3f...", "interval_ms": 1500, "max_points": 2000}
```

```json
{"type": "plot_snapshot", "run_id": "0d3f...", "plot_id": "browser-8050", "kind": "browser",
 "sequence": 42, "final": false,
 "data": {"V": [0.1, 0.2], "I": [0.002, 0.004],
          "fit_1": {"fit_result": [0.0021, 0.0039], "fit_params": {}, "y_axis_name": "I"}}}
```

- `data` for `browser` plots is the unchanged response of the Dash `/data?max_points=N`
  endpoint, so the existing OASIS Plotly code keeps working.
- `kind` is `browser`, `qt` or `pid`. These come from `/data`, `/gui/plot-caches` and
  `/gui/pid-plots`.
- Each snapshot replaces the previous one for that `plot_id`. `sequence` increases
  for each plot and is used to discard older snapshots.
- Latest wins: the agent keeps at most one unsent snapshot per plot and never lets
  plot data delay a `command_result`.
- `final: true` is the last snapshot of a run. The broker keeps it after the run ends.
  This replaces the 30-second refresh period, after CAMELS has flushed the plot.
- Snapshots are for display only. The complete data stays in CAMELS and NOMAD.

## 9. Internal broker API

This API is reachable only on the internal Docker network and is not routed by
Nginx. Callers authenticate with the service secret in the `X-Broker-Secret` header. They must already have authorized
the NOMAD user for the device.

| Request | Purpose |
|---|---|
| `POST /internal/devices` | Create a device and its first pairing code |
| `GET /internal/devices?owner=` | List devices with their online state |
| `POST /internal/devices/{device_id}/pairing-code` | Issue a new pairing code; pairing replaces the old token |
| `DELETE /internal/devices/{device_id}` | Delete a device, revoke its token, close its connection with `4401` |
| `POST /internal/devices/{device_id}/commands` | Send a command and wait for `command_result` |
| `GET /internal/devices/{device_id}/state` | Online state and latest `status` |
| `GET /internal/devices/{device_id}/plots?run_id=` | Latest snapshots; also renews the subscription |

The broker ends a subscription 10 seconds after the last plot request, so a closed
browser tab stops the data flow.

## 10. Security requirements

- Tokens, API keys and full variable values never appear in logs. Audit records hold
  the user, device, protocol, operation, time and result.
- The agent runs only whitelisted operations. It never executes received code,
  commands or URLs.
- Only protocols marked with `oasis_remote_control` are visible or can be started.
- Remote use of OASIS never replaces local instrument limits or emergency stops.

## 11. Changes required in the current code

### CAMELS

- Add the agent, remote-mode settings and pairing to the settings window.
- Start uvicorn only when local API access is wanted, and bind it to `127.0.0.1`
  in remote mode. It currently binds to `0.0.0.0` (`api/api.py`, `start_server`).
- Add a stop route that calls `stop_protocol` through a Qt signal.
- Return 409 when a measurement is running, and deduplicate `protocol_uuid`.
- Report failures after `start_protocol_signal`, because `run_protocol` can return
  early when `build_protocol` fails.
- Add `run_id` to the measurement status snapshot.
- Replace the blocking `urlopen` in the plot proxy with `asyncio.to_thread`, so it
  cannot block heartbeats.
- Add `websockets` to the `api` dependencies.

### OASIS

- Add the broker service, its Nginx WebSocket location and device storage.
- Send `request_camels` and both Temporal activities through the internal broker API.
- Create `run_id` in the workflow, and pass it through every activity retry.
- Replace API-key connections in the setup page with device pairing.
- After the plot migration, remove the Dash HTML rewriting and iframe code.

## 12. Open questions

1. Can users other than the one who started a measurement stop it? A safe default is
   that every user with access to the device can.
2. Should an operator at the lab computer be able to block remote starts temporarily?
3. Does `stop_protocol` need a confirmation step for particular instruments?
