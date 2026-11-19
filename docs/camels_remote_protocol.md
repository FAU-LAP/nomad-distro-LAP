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
 "operations": ["list_protocols", "start_protocol"], "features": ["changes", "live_update"],
 "measurement": {"state": "running", "run_id": "0d3f...", "protocol_name": "IV_Sweep"}}
```

`features` lists optional behaviour: `changes` means the agent sends `changed`
(see below). Agents without `features` are polled.

### `changed`

```json
{"type": "changed"}
```

Sent by the agent when a plot was opened, closed or changed, or the measurement
status changed. It carries no data. The agent checks for changes every 0.1 s
in-process and sends `changed` at most every 0.2 s, so nothing is sent while
nothing changes. The broker wakes up the waiting `/changes` requests (section 9);
OASIS then fetches the new data with `get_live_update`.

The connection uses permessage-deflate compression (agent: `compression="deflate"`,
broker: uvicorn default).

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
agent lists the operations it supports in `hello`. `{protocol}` is URL-encoded.
Paths start with `/api/v1`. Payload fields not listed are ignored.

| Operation | Payload | CAMELS route | Changes state |
|---|---|---|---|
| `get_service_info` | | `GET /info` | |
| `list_protocols` | | `GET /protocols` | |
| `get_protocol_schema` | `protocol` | `GET /protocols/{protocol}/schema` | |
| `get_protocol_definition` | `protocol` | `GET /protocols/{protocol}/JSON` | |
| `get_run_status` | `run_id` | `GET /protocols/results/{run_id}` | |
| `get_measurement_status` | | `GET /gui/measurement-status` | |
| `get_plot_count` | | `GET /gui/plots` | |
| `get_plot_data` | `cursor` (object, optional) | `GET /gui/plot-caches?cursor=` | |
| `get_pid_plots` | `protocol` | `GET /gui/pid-plots?protocol_name=` | |
| `get_live_update` | `cursor` (object, optional) | `GET /gui/live?cursor=`: `{"status": ..., "plots": ...}` | |
| `save_protocol_parameters` | `protocol`, `variables` | `POST /protocols/{protocol}/parameters` | yes |
| `start_measurement` | `protocol`, `variables`, `run_id` | `POST /actions/run/protocols/{protocol}?protocol_uuid={run_id}` | yes |
| `stop_measurement` | | `POST /actions/stop` | yes |
| `close_plots` | | `POST /gui/plots/close` | yes |
| `close_plot` | `plot_id` (id from the plot data) | `POST /gui/plots/{plot_id}/close` | yes |

Operations that change state are refused with `paused` while remote control is
paused on the lab computer (**Tools > OASIS Remote Control...**). Reading stays
possible, so OASIS can still watch the measurement.

Error codes. The message is CAMELS' own reason where it has one, for example
`Unknown protocol variables: x`.

| Code | Source | HTTP status in OASIS |
|---|---|---|
| `device_offline` | Broker: no active connection (`rejected`), or it was lost while waiting (`unknown`) | 503 |
| `timeout` | Broker: no answer before the deadline (`unknown`) | 504 |
| `unknown_operation` | Agent: not in the whitelist, for example an older CAMELS | 501 |
| `not_found` | Route returned 404, including protocols that are not exposed | 404 |
| `invalid_parameters` | Route returned 400 or 422 | 400 |
| `busy` | Route returned 409: a measurement is running | 409 |
| `paused` | Agent: remote control is paused on the lab computer | 423 |
| `internal` | Other failures. The message contains no secrets or stack traces | 502 |

## 6. Delivery rules

These rules prevent a retry from starting a second measurement.

1. **OASIS creates `run_id`.** The dashboard creates the UUID when it starts the
   Action and passes it in `CAMELSMeasurementInput`, so every retry of the start
   activity uses the same value.
2. **CAMELS deduplicates `run_id`.** The agent remembers the last 200 started
   `run_id`s; a known one returns the earlier result with `"duplicate": true` and
   starts nothing. Starts are handled one at a time. CAMELS also refuses a start
   with `busy` while a measurement is being prepared, running or paused.
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

CAMELS sends data, not images. Every open 1D plot window keeps a thread-safe
copy of its points and appearance (`nomad_camels/main_classes/remote_plot_cache.py`).
The browser asks `/dashboard/live` with a cursor and receives the measurement
status and only the points it does not have yet. For a computer whose agent
reports changes, the request waits until CAMELS sends `changed` (at most 15 s),
so new points appear within about 0.1 s. Direct API connections and older agents
are polled (every second while measuring, otherwise every three seconds).
The cursor:

```json
{"cursor": {"140221": {"generation": "a1b2c3d4-0", "since": 120, "version": 3}}}
```

```json
{"open_plot_count": 1, "plots": [{
  "id": "140221", "generation": "a1b2c3d4-0", "version": 3,
  "reset": false, "start": 120, "end": 124, "more": false,
  "title": "IV", "x_label": "U", "y_label": "I", "y2_label": "",
  "x_log": false, "y_log": false, "y2_log": false,
  "background": "#ffffff", "foreground": "#000000",
  "x": [1.2, 1.3, 1.4, 1.5],
  "traces": [{"name": "I", "axis": "y", "y": [0.1, 0.2, 0.3, 0.4], "mode": "markers",
              "marker": {"symbol": "circle", "size": 10, "color": "#1f77b4"}}],
  "fits": [{"name": "fit_I", "axis": "y", "x": [], "y": [], "mode": "lines", "line": {}}]}]}
```

- `generation` changes when the plot is cleared, and differs between plot windows.
  With an outdated generation or a position that is no longer kept (finite number
  of points in CAMELS), `reset` is true and the plot starts from its first point.
- A reply holds at most 20000 numbers for all plots together; `more` asks the
  client to request again at once. A reply stays far below the 1 MiB limit.
- `traces` carry the curve styles read from pyqtgraph (color, symbol, line style,
  visibility) as Plotly fields, so OASIS draws the plot like CAMELS. `fits` are
  sent only when `version` changed.
- `info` describes the run the plot belongs to; it is sent with `fits`:
  `{"protocol": "IV", "started": "2026-11-19T14:03:22+01:00", "ended": null,
  "state": "running", "variables": {"n": 3}}`. `state` is `running`, `finished`,
  `aborted` or `failed`. CAMELS shows the same information in a collapsible box in
  the top right corner of the plot window; OASIS shows it above each plot, with the
  state and a button that closes only this plot (`close_plot`).
- Plot data is for display only. The complete data stays in CAMELS and NOMAD.
- The Dash browser plots (`/gui/browser-plots/...`) are not available for remote
  computers.

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
| `GET /internal/devices/{device_id}/changes?after=&timeout=` | Long poll, see below |

`/changes` returns `{"seq", "online", "push"}`. `seq` counts the `changed`
messages of the device (and its connects and disconnects). The request answers at
once if `seq` differs from `after` (or `after` is missing), otherwise after the
next change or `timeout` seconds (at most 30). `push` is false when the connected
agent does not report changes; then the request never waits.

## 10. Security requirements

- Tokens, API keys and full variable values never appear in logs. Audit records hold
  the user, device, protocol, operation, time and result.
- The agent runs only whitelisted operations. It never executes received code,
  commands or URLs.
- Only protocols marked with `oasis_remote_control` are visible or can be started.
- Remote use of OASIS never replaces local instrument limits or emergency stops.

## 11. Implementation status

Done:

- Changes are pushed (`changed`, `/changes`, `get_live_update`); compression on
  the connection. The local CAMELS API listens on `127.0.0.1` unless
  **Allow API access from other computers** is checked in the settings.
- Run info of each plot in CAMELS and OASIS; closing a single plot from OASIS.

- CAMELS: agent, pairing dialog with pause switch, stop route, `busy` (409) for a
  second start, `run_id` deduplication in the agent, plot data copies with cursor
  and appearance, `websockets` in the `api` dependencies.
- OASIS: broker service with Nginx location, device pairing on the setup page,
  `request_camels` and both Temporal activities through the broker
  (`nomad_camels_oasis/remote.py`), `run_id` created before the start, Live plots
  section, stop button.

Open:

- The `status` messages of section 7. Instead, `changed` (section 4) wakes up
  OASIS, which reads the status with `get_live_update`.
- Remove the Dash iframe code once no direct API connections are used.

## 12. Decisions

1. Only the owner of a computer (the OASIS user who added it) can see it, read its
   protocols and plots, start, stop or change parameters. Other users get 404.
2. Staff at the lab computer can pause remote control in CAMELS; OASIS can then
   only watch (`paused`).
3. Starting and stopping need a second click in the dashboard.
