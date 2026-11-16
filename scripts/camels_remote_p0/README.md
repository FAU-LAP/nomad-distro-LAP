# P0 network check for CAMELS remote control

This check answers one question before any broker or agent is built: **can the
lab computer keep an outbound WSS connection to the OASIS server, and can the
server send messages down that connection?**

The tools only echo test messages. They do not touch CAMELS, instruments or
OASIS data, and the Docker stack is separate from the OASIS stack.

## 1. On the OASIS server

You need Docker and a free port 443. If OASIS already uses that port, stop its
proxy during the test or set `P0_PORT`. A different port does not prove that
443 works, so repeat the test on 443 if the result will be used for the decision.

Run these commands from this directory, replacing `oasis.example.org`:

```bash
export P0_TOKEN="$(openssl rand -hex 32)"
echo "$P0_TOKEN"   # Copy this for the lab computer.

mkdir -p tls
# MSYS_NO_PATHCONV=1 stops Git Bash on Windows from rewriting "/CN=..." as a path.
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout tls/server.key -out tls/server.crt \
  -subj "/CN=oasis.example.org" \
  -addext "subjectAltName=DNS:oasis.example.org"

docker compose up
```

For a server addressed by IP, use `subjectAltName=IP:<address>`. The `tls/`
directory is ignored by Git. If the server has a certificate from a trusted CA,
copy it to `tls/server.crt` and `tls/server.key` instead, and the client will
not need `--ca-file`.

## 2. On the lab computer

Copy `echo_client.py` and `tls/server.crt` (never `server.key`), then run in
PowerShell:

```powershell
python -m venv p0-venv
.\p0-venv\Scripts\python -m pip install "websockets>=15,<18"

$env:P0_TOKEN = "<token from step 1>"
.\p0-venv\Scripts\python echo_client.py wss://oasis.example.org/camels-remote/p0 --ca-file server.crt
```

The full run takes about 34 minutes. Add `--soak-minutes 0` for a first check of
about 4 minutes. Run a full check at least once on the real lab network.

## 3. What the results mean

| Check | What a pass proves |
|---|---|
| `handshake` | DNS, TCP, TLS, the certificate, the WebSocket upgrade and the `Authorization` header all work |
| `payload ...` | Plot snapshots of this size can pass in both directions |
| `server message after ... of silence` | OASIS can reach the lab computer on an idle channel |
| `soak ...` | The connection stays open with the planned 20-second heartbeat |

A failed idle check at 60 or 120 seconds is not a blocker when the soak check
passes, because the real agent sends a heartbeat every 20 seconds. Any other
failure must be solved first, and the printed message names the network layer.

The exit code is 0 only when every check passes. The check is only valid when
the client runs on the real lab network, because a run on the server or on another
network says nothing about the lab's outbound path. If the network forces a TLS
inspection proxy, the `handshake` check shows a certificate error; ask IT for an
exception or for the proxy CA.

## 4. Clean up

```bash
docker compose down
rm -rf tls
```
