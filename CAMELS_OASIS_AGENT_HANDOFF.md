# CAMELS OASIS Remote Control Project Handoff

## Purpose

This document is a working handoff for another coding agent. It summarizes the
current CAMELS to NOMAD OASIS remote-control work, the design decisions already
made, repository state, unresolved issues, and the commands used during local
development and network tests.

The project goal is to let a signed-in NOMAD OASIS user securely connect to a
CAMELS instrument computer, select only CAMELS protocols deliberately exposed
for remote control, edit allowed parameters, start a measurement, and monitor
its status and plots remotely.

## Current Architecture

```text
OASIS user browser
  -> NOMAD OASIS GUI v2 Dashboards and Actions
  -> OASIS CAMELS bridge backend
  -> authenticated CAMELS FastAPI service on the instrument computer
  -> CAMELS GUI, protocol runner, instruments, Browser Plot data
```

The CAMELS API service listens locally on a configurable TCP port. During the
tests it was commonly port `5000`, while earlier local development also used
`8000`. OASIS stores a CAMELS connection after validating its API key. The
connection is user-owned in OASIS and its API key is encrypted at rest.

The intended network direction is **OASIS -> CAMELS**. CAMELS does not need to
access the OASIS computer. For a remote deployment, CAMELS should expose its
API only to the OASIS server through firewall rules, a private/VPN interface,
or equivalent network controls.

## Repositories and Branches

### CAMELS

Local repository:

```text
C:\NOMAD_CAMELS\NOMAD-CAMELS
```

Current working branch:

```text
CAMELS_remote_WebSocket
```

Its GitHub upstream remote is:

```text
origin = https://github.com/FAU-LAP/NOMAD-CAMELS.git
```

The branch exists on the official GitHub repository and was pushed successfully.
The latest synchronization action merged current `origin/main` into this branch.
The merge commit is:

```text
1aa8a924 Merge remote-tracking branch 'origin/main' into CAMELS_remote_WebSocket
```

The previous feature commit is:

```text
abaa32e8 Persist CAMELS API keys across restarts
```

There is also a `personal` Git remote configured as
`https://github.com/Lao218/NOMAD-CAMELS.git`. A push to it failed with
`Repository not found`; do not rely on it unless a personal fork is created or
the remote URL is corrected.

### OASIS distribution repository

Local repository:

```text
C:\Users\mo67giru\Desktop\Nomad_Oasis_RemoteCAMELS\nomad-distro-template
```

Current branch created for the next iteration:

```text
OASIS_remote_WebSocket
```

The preceding OASIS development branch was:

```text
Oasis_browser_remote_camels
```

The OASIS repository uses the user's fork as `origin` and the upstream
FAIRmat distribution repository as `upstream`. The new OASIS branch may still
need to be published from VS Code with **Publish Branch**, unlike the CAMELS
branch which already exists on origin.

### Meaning of origin

`origin` is only the local nickname Git gives to a remote repository. In the
CAMELS VS Code dialog, syncing with
`origin/CAMELS_remote_WebSocket` means pulling and pushing the remote branch
of that exact name. It does not switch to `main`, delete the feature branch, or
automatically merge anything new from `main`.

## OASIS Implemented Work

### Authentication and access control

The target behavior is: an unauthenticated browser must not be able to open
the CAMELS connection setup dashboard, read saved API connections, inspect
protocols, save parameters, or start experiments.

Important observation: the standard NOMAD GUI v2 Actions page already displays
an **Authentication required** card after logout. Custom Dashboard pages used a
separate FastAPI bridge and initially did not consistently share this behavior.
Several attempted fixes were made, including returning the same lock page from
the bridge for 401 responses and clearing browser authentication state at
logout. The persistent issue was that cached/custom dashboard content could
remain usable after logging out even when Actions were locked.

Relevant logic added in the OASIS bridge includes an exception handler similar
to:

```python
@app.exception_handler(HTTPException)
async def camels_page_authentication_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and page_path.endswith(("/camels/setup", "/camels/dashboard")):
        return HTMLResponse(AUTHENTICATION_REQUIRED_PAGE, status_code=401, headers=exc.headers)
```

And a logout endpoint intended to clear browser state:

```python
@app.post("/logout", status_code=204)
def clear_browser_authentication() -> Response:
    response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    response.delete_cookie("Authorization", path=...)
```

This was not a fully reliable solution for the GUI v2 dashboard catalogue and
custom UI plugin lifecycle. The robust long-term solution is to use the same
NOMAD GUI v2/plugin authentication guard as Actions, or have all sensitive
dashboard APIs strictly validate the active NOMAD request on every request and
make the frontend show the lock page for each 401 response. Never rely on only
client-side hiding or cache clearing for authorization.

### Per-user encrypted CAMELS connections

OASIS bridge credentials are saved server-side, not in browser local storage.
The bridge stores connections in an encrypted settings file, currently under a
Docker volume path similar to:

```text
.volumes/fs/camels-bridge/settings.json
```

The data structure was moved from legacy global connections to per-user buckets:

```python
users[user_id] = {
    "connections": connections,
    "active_connection_id": active_connection_id,
}
```

Legacy global entries are preserved as `legacy_unassigned` for manual migration
but are intentionally not used for API requests.

The bridge uses one global CAMELS API key per saved CAMELS connection, rather
than duplicating the same key once per protocol. CAMELS remains the authority
for which protocols may be discovered and controlled: its per-protocol
`oasis_remote_control` setting determines exposure.

### Setup Dashboard

The custom CAMELS setup page was changed to:

- Require authentication before connection operations.
- Display the same black lock-card visual styling as the Actions page where
  possible, with a working sign-in flow.
- Center the protected setup interface.
- Change saved CAMELS connections from an unstructured list to a table.
- Display connection name, host, local IPv4 addresses, API port, creation time,
  and delete control.
- Make delete controls functional again.
- Replace destructive API `DELETE` operations with POST actions in the bridge
  UI/routes where requested.
- Add a delete-all confirmation.
- Use a human-readable backend `message` instead of dumping raw JSON error.
- Refresh protocol choices immediately after saving a new connection.
- Link from setup into GUI v2 dashboard routes.

When CAMELS validates the API key but has no protocol currently marked
**Expose to OASIS remote control**, the connection should still be saved.
The user-facing message should explain that the connection has no exposed
protocol yet and direct the user to CAMELS protocol settings. It should not say
the connection cannot be added to the account.

### Protocol discovery and control

The OASIS bridge supports a small compatibility API including health checks,
protocol list/schema/definition retrieval, start run, and run status. A
connection discovery call is conceptually:

```python
GET {base_url}/api/v1/protocols
Authorization: Bearer {api_key}
```

The protocol catalogue checks current global CAMELS connection records first,
then old protocol-scoped connection records. This prevents obsolete protocol
keys from shadowing a newer working global connection.

The setup UI can select a discovered protocol, fetch editable parameters, save
parameters to CAMELS, launch a NOMAD Action, and poll Action state. Dashboard
initiated Actions were explicitly changed so `upload_id` may be `None`; they
are user-owned but do not originate from an ELN upload.

### Browser Plot and dashboard monitoring

Original Browser Plot behavior used an iframe/direct URL such as
`http://host.docker.internal:<port>`. This only works when the OASIS Docker
container and CAMELS process are effectively on the same host. It cannot reach
a Browser Plot on a separate CAMELS computer through that address.

The preferred implementation is now:

1. CAMELS provides an authenticated API proxy to the local Browser Plot/data.
2. OASIS obtains plot data via the CAMELS API.
3. OASIS renders the result with Plotly rather than directly embedding a
   remote Browser Plot iframe.

CAMELS has an authenticated Browser Plot proxy route conceptually like:

```python
GET or POST /api/v1/gui/browser-plots/{protocol_name}/{port}/{plot_path}
```

It verifies the API key and the protocol exposure, accepts only a port in the
currently running CAMELS protocol's active `web_ports`, and internally forwards
to `http://127.0.0.1:{port}`. This preserves the Dash application as
localhost-only while allowing the authenticated OASIS bridge to reach it.

The plot `/data` endpoint was extended to optionally accept `max_points` and
evenly downsample large arrays while keeping both endpoints. This prevents
unbounded plot responses during a long run. OASIS additionally limits display
payloads to approximately 2000 points, including saved fit-result traces.
This is only a presentation limit: lossless data remains in CAMELS/NOMAD.

Fit curves were initially missing because only primary raw traces were being
read. The OASIS plot payload code was extended to preserve/return stored
`fit_result` arrays so fitted traces can be drawn with the main plot.

Polling is intended only while a measurement is active. The UI stops high-rate
refresh after completion while retaining the final rendered plot snapshot.
The latest OASIS change also adds a **30 second finalization grace period**:
once CAMELS reports a run as finished, OASIS continues plot and PID refreshes
for 30 seconds at the normal 1.5 second period, then stops. This addresses the
case where CAMELS marks the run complete before the Browser Plot finishes its
final rendering/data flush.

Relevant OASIS file for the 30 second change:

```text
nomad_camels_oasis/app.py
```

Key names added:

```javascript
const BROWSER_PLOT_FINALIZATION_MS = 30000;
function shouldRefreshCamelsPlots() { ... }
```

This syntax/build check passed:

```powershell
python -m py_compile nomad_camels_oasis\app.py
git diff --check
```

After modifying OASIS frontend/backend assets in Docker, rebuild/restart:

```powershell
docker compose up --build
```

### Active protocol selection

The dashboard originally defaulted by protocol name/order, so entering the
dashboard while `Demo_Protocol_2` was running could show `Demo_Protocol_1`.
Desired behavior:

- If a CAMELS protocol is currently running, select and open that protocol.
- If no protocol is running, retain the existing name/order default.

This was implemented during the earlier dashboard work. If it regresses,
inspect the dashboard's protocol-status/load order before changing the plot
code.

### Docker and Nginx related changes

`.gitignore` changes added rules for encrypted bridge credentials, transient run
logs, and the local `nomad-gui/` checkout so secrets, generated files, and the
independent GUI repository do not accidentally enter the distribution Git
repository:

```gitignore
.volumes/fs/camels-bridge/settings.json
.run-logs/
/nomad-gui/
```

`.dockerignore` reduces Docker build context while allowing the required source
trees to be copied. It excludes the local GUI repository metadata,
`node_modules`, build output, local `.env`, encrypted volumes, and logs.
It is useful in the current Docker-based local build. A future non-Docker
server deployment may not need a `.dockerignore`, but secret/log exclusion
remains desirable in whatever deployment workflow is used.

An Nginx `/nomad-oasis/api/v1/info` cache-control change was made and then
identified as no longer useful. It was intentionally reverted/should not be
reintroduced unless GUI v2 dashboard catalogue caching is again demonstrated
to be the cause of stale data.

## CAMELS Implemented Work

### API service and safe GUI interaction

CAMELS was extended with a FastAPI API intended for OASIS access. Important
features include:

- `/api/v1/health` is an unauthenticated liveness endpoint returning API
  version data.
- `/api/v1/info` requires API-key authentication and reports non-secret
  service identity information: hostname, local IPv4 addresses, API port,
  version, and API start time.
- Protocol lookup accepts either the protocol dictionary key or current display
  name.
- A protocol must exist, have `oasis_remote_control=True`, and match the API
  key's protocol scope if that key is scoped. Otherwise the API returns a
  non-enumerating 404.
- Protocol-scoped keys are rejected for legacy global-control endpoints.
- Run responses include `run_id`, `status_url`, and `result_url` while keeping
  legacy `check_protocol_status_here` compatibility fields.
- The run-status database stores protocol name and creation metadata.

CAMELS GUI state is not read directly from FastAPI's thread. `MainApp_v2.py`
maintains a lock-protected plain Python measurement-status snapshot:

```python
self._measurement_status_lock = threading.Lock()
self._measurement_status = {
    "status": "Idle",
    "protocol_name": "",
    "progress": 0.0,
    "elapsed_seconds": 0.0,
    "remaining_seconds": None,
}
```

The GUI updates this snapshot; FastAPI safely reads the snapshot instead of
accessing Qt widgets such as a progress bar from a different thread.

Plots likewise register a thread-safe `remote_plot_cache` with the FastAPI
thread while open and unregister it when closing. This prevents the API from
trying to manipulate Qt/Dash plot widgets directly.

### CAMELS API-key persistence

File changed:

```text
nomad_camels/frontpanels/settings_window.py
```

The prior key design stored only an API-key hash. It could validate a supplied
key but could not recover/display it after restarting CAMELS.

The new implementation:

- Adds a `protected_key` field to the existing `CAMELS_API.db` database.
- Uses Windows DPAPI under the current user account to encrypt the original
  generated API key before storing it.
- Keeps the original hash for validation.
- Reloads and displays/copies the existing saved key when opening settings.
- Adds a **Keep current API key** checkbox, enabled by default.
- While the checkbox is checked, key regeneration is disabled; uncheck it to
  intentionally create/replace a key.

Existing old hash-only records cannot reveal their former plaintext key. For
those records, generate one new key once and update the saved OASIS connection.

This implementation was syntax checked with the CAMELS virtual environment and
the DPAPI encrypt/decrypt roundtrip passed.

### CAMELS API availability and firewall findings

The FastAPI service was successfully verified locally on the CAMELS machine:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5000
Invoke-WebRequest http://127.0.0.1:5000/api/v1/health -UseBasicParsing
```

Expected health response:

```json
{"status":"ok","api_version":"v1"}
```

One test showed the service listening on `0.0.0.0:5000`, which accepts network
connections at the OS level.

An elevated PowerShell firewall rule was successfully created on the CAMELS
computer to allow only the OASIS computer at `10.188.162.32` to reach CAMELS
port `5000`:

```powershell
New-NetFirewallRule -DisplayName "CAMELS API from OASIS" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000 `
  -RemoteAddress 10.188.162.32
```

However, cross-machine tests still failed even though local health succeeded.
For example, from the OASIS machine:

```powershell
Test-NetConnection -ComputerName 10.188.162.36 -Port 5000
```

returned `PingSucceeded : True` but `TcpTestSucceeded : False`.

Interpretation:

- `PingSucceeded=True` means basic ICMP reachability is available.
- `TcpTestSucceeded=False` means a TCP connection to the requested API port
  could not be made. It is not an API-key or OASIS application-level problem.

Likely remaining causes are the active Windows firewall profile/interface,
incorrect target IP, endpoint security, a VPN/school network policy blocking
peer-to-peer TCP, or wireless client isolation. The OASIS machine's outbound
firewall profile showed `DefaultOutboundAction : NotConfigured`, so no local
outbound block was apparent. A VPN alone did not solve the timeout.

Recommended network diagnosis order:

1. On CAMELS machine, verify `Get-NetTCPConnection -State Listen -LocalPort 5000`.
2. On CAMELS machine, verify local `/api/v1/health`.
3. From OASIS machine, use `Test-NetConnection <CAMELS-IP> -Port 5000` with the
   exact syntax (a missing space before `-Port` creates an unrelated error).
4. Temporarily create a narrowly scoped inbound firewall allow rule for the
   actual OASIS source IP.
5. Confirm the CAMELS IP is the active Ethernet/WLAN/VPN adapter IP, not a
   stale address.
6. If TCP still fails while ping works, involve school network/IT: client
   isolation or port filtering is outside the application code.

Never expose the CAMELS API unrestricted to the public internet. Binding to
`0.0.0.0` means the service listens on all local network interfaces; whether it
is actually reachable depends on the firewall/network. A narrow firewall allow
rule is the appropriate minimum for the current design.

## CAMELS Driver Work

The user is building a driver for the magnetic power supply
`FHG MTN 2800 650` / `MTN 2800-650`, which is used to control magnetic field.
The desired CAMELS-recognized channels include:

```text
magnetic_polarity
magnetic_power_on
```

The driver-builder output initially had an incorrect nested structure:

```text
C:\Nomad_drivers\CAMELS_drivers\FHG_mcn_2800_650\FHG_mcn_2800_650\
```

CAMELS needs the driver package directory directly under its configured local
driver root, and the import package must match the generated driver module.
The user inspected another known-good driver (`lab_course_magnet`) as the
template. A new driver folder was requested with this exact name:

```text
Labcourse_Magnetic_powersupply_FHQ_MTN_2800_650
```

The package must include the generated Python module, ophyd module,
`__init__.py`, `pyproject.toml`, README, and LICENSE in the same structural
pattern as a recognized local driver. It may also need installation through
CAMELS Manage Instruments or editable installation in the exact CAMELS Python
environment, depending on CAMELS local-driver discovery configuration.

An import test showed a `ModuleNotFoundError` because the module path used in
the test did not match the actual generated files. The real contents shown were
similar to:

```text
nomad_camels_driver_fug_mtn_2800_650\
  fug_mtn_2800_650.py
  fug_mtn_2800_650_ophyd.py
  __init__.py
```

Use this exact module naming when testing/importing. Before changing driver
code, compare the full package layout and `pyproject.toml` entry points with
`lab_course_magnet`.

The instrument's serial example uses PySerial with a COM port, baud rate 9600,
8 data bits, no parity, one stop bit, and `*IDN?\r\n`. The actual device manual
and device-specific command set must be verified before sending production
commands.

## CAMELS General Runtime Issues

### EPICS preset / baud-rate issue

After recreating the virtual environment, CAMELS showed errors such as:

```text
ValueError: Type names and field names must be valid identifiers: ''
EPICS PV row 2 needs a PV Short Name.
```

This is not primarily a baud-rate issue. The trace shows the `epics_instrument`
driver creates an Ophyd component from an EPICS PV row with an empty or invalid
short name. CAMELS then cannot load the most recent preset, such as
`Temperature-dependent measurement`.

Fix:

1. Open Manage Instruments / Configure Instruments.
2. Find the EPICS instrument configuration.
3. Give every PV row a nonempty valid Python-identifier short name, especially
   row 2. Use letters, digits, and underscores; do not start with a digit.
4. Save and reload the preset.

### Subprotocol loop and fitted slope issue

A parent protocol runs a subprotocol successfully once, but fails when the
subprotocol is invoked inside a loop. The missing variable error names a fit
slope such as:

```text
1/linear_SMU_measV2_minus_SMU_measV1_v_SMU_source_mes1_vdP1_slope
```

The child protocol's `Set Variables (calculate_values)` step refers directly to
the fitted slope. A single child run works. Repeated child runs in a loop fail.

Important distinction: parent protocol **Variables In/Out** cannot solve this
by itself because the child's calculation occurs *inside* the child protocol;
parent outputs happen only after the child completes.

Likely source: a CAMELS lifecycle/namespace issue involving repeated Run
Subprotocol execution and fit results. The relevant generated mechanisms are:

- `simple_sweep.py` calls `get_fit_results(all_fits, namespace, True, stream)`.
- `helper_functions.get_fit_results` adds fit-result names to the namespace and
  resets the fit object.
- `protocol_builder.run_subprotocol_string` runs the child plan, exports
  outputs, and handles fit results.

The `1/` prefix suggests a loop/substream namespace. A durable fix should be
implemented only after reproducing against the generated child and parent
Python. Likely directions are rebinding/reinitializing the child fit/plot
stream per loop invocation or creating stable aliases for the child fit results
before the calculation step reads them. Do not blindly rename the child formula
or alter it unless a controlled workaround is acceptable.

## Current CAMELS Main Merge Resolution

When synchronizing `CAMELS_remote_WebSocket` with `origin/main`, merge
conflicts occurred in:

```text
nomad_camels/MainApp_v2.py
nomad_camels/main_classes/plot_proxy.py
```

Resolution:

- `MainApp_v2.py`: retained the feature branch's `SafeDict` behavior for
  protocol/loop values in output filename formatting while preserving the
  upstream compatible surrounding changes.
- `plot_proxy.py`: accepted the latest upstream main process-based ZMQ proxy
  implementation (`run_zmq_proxy`). The automatically merged callers now use
  this newer entry point.

Both files passed `py_compile`. `git diff --check --cached` reported existing
upstream trailing whitespace in `README.md` and `plot_proxy.py`; this was not
changed merely to avoid unrelated formatting edits.

## Running the Local OASIS Stack

From the OASIS distribution repository with Docker Desktop running, the usual
local startup is:

```powershell
docker compose up --build
```

GUI v2 local URL:

```text
http://localhost/nomad-oasis/gui/v2/
```

The user also created a short local readme named approximately:

```text
read me_OASISremote_CAMELS
```

It contains the lightweight local startup and connection procedure. Verify its
actual path/name before relying on it.

## Git Usage and Syncing on Another Computer

To check branch and remotes in any repository:

```powershell
git branch --show-current
git remote -v
git status
```

The command must run inside the repository, not merely its parent directory.
For CAMELS use:

```powershell
cd C:\NOMAD_CAMELS\NOMAD-CAMELS
```

On a second computer that has an older clone, update the branch with:

```powershell
git fetch origin
git switch CAMELS_remote_WebSocket
git pull origin CAMELS_remote_WebSocket
```

If the branch is not present locally yet:

```powershell
git fetch origin
git switch --track origin/CAMELS_remote_WebSocket
```

The GitHub branch page showing `Behind 15 / Ahead 17` is a comparison against
the repository default `main`. It does not mean the feature branch lacks its
own pushed commits. The 15 commits "behind" are main commits not yet merged;
this was addressed for `CAMELS_remote_WebSocket` through merge commit
`1aa8a924` at the time of the most recent synchronization. Recheck with
`git fetch origin` before assuming the count remains current.

VS Code **Sync Changes** does a pull followed by a push for the branch's tracked
upstream. The prompt naming `origin/CAMELS_remote_WebSocket` is expected and
safe when the worktree has no uncommitted changes.

Git author configuration is separate from GitHub branch ownership. If Git
shows an identity error or auto-detected author, configure it once:

```powershell
git config --global user.name "Lao218"
git config --global user.email "YOUR_GITHUB_EMAIL"
```

Use the email attached to the intended GitHub account. Do not amend existing
commits solely for author cosmetics without confirming it is desired.

## Generated Change Documentation

The CAMELS repository contains generated/manual handoff artifacts:

```text
CAMELS_main_branch_change_notes.md
CAMELS_main_branch_changes.patch
```

They describe and export differences compared with main. They were included in
commit `abaa32e8`, even though the original intent was primarily to commit the
settings-window API-key persistence code. Keep them if they are useful as
review material; otherwise remove them only in a deliberate future commit, not
by rewriting published history without a team decision.

## Security Recommendations

1. Keep CAMELS API keys only in OASIS backend encrypted storage. Never put
   plaintext keys in browser local storage, frontend code, logs, screenshots,
   or Git.
2. Maintain per-user OASIS connection buckets; do not fall back to legacy global
   connection records for requests.
3. Enforce authentication and authorization at every bridge endpoint, including
   read-only protocol/plot routes. A hidden UI card is not security.
4. CAMELS should disclose only protocols explicitly marked for OASIS remote
   control. Default must be not exposed.
5. Limit CAMELS incoming network access to known OASIS server IPs/VPN. Avoid
   public internet exposure.
6. Use TLS or a trusted private VPN/tunnel before production deployment.
7. Record non-secret audit events: account, connection ID, protocol, action,
   timestamps, success/failure. Do not log API keys.
8. Add explicit safety interlocks/permissions for instruments where remote start
   can cause hazards.

## Practical Deployment Recommendations

- Deploy OASIS on the school server with managed database/secret storage rather
  than a local encrypted JSON file when moving beyond the prototype.
- Keep each CAMELS workstation a controlled API client with a unique API key
  and firewall allow list.
- Ensure stable workstation hostname/DNS or reserved IP; student laptops should
  not rely on changing DHCP addresses.
- For live plots, use API `/data` plus OASIS Plotly rendering. Do not use direct
  remote iframes for cross-machine Browser Plot access.
- Keep live polling active only during a run, downsample display payloads, keep
  a final snapshot, and use the 30-second finalization period.
- For multi-user/multi-experiment scaling, move from polling to a WebSocket or
  server-sent-events design. That is the motivation for the newly created
  `CAMELS_remote_WebSocket` and `OASIS_remote_WebSocket` branches.

## Suggested Next Tasks

1. Confirm `OASIS_remote_WebSocket` is published to the correct user fork.
2. Verify the CAMELS API key persistence UI manually: restart CAMELS, reopen
   settings, confirm old key is visible, confirm keep-key checkbox prevents
   regeneration, and confirm OASIS can still authenticate.
3. Re-test OASIS plot finalization with a protocol whose Dash plot flushes late.
4. Resolve the school-network TCP reachability issue before debugging OASIS API
   application behavior across two computers.
5. Inspect/reproduce the looped subprotocol fit-slope bug with saved generated
   code and add a regression test before modifying CAMELS internals.
6. Implement WebSocket/SSE measurement and plot updates on the new branches
   after defining message formats, authorization checks, reconnection behavior,
   and final-snapshot behavior.
7. Replace prototype JSON credential storage with production-grade database
   encryption/secret management before broad student deployment.

