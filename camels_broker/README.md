# CAMELS remote broker

The broker connects OASIS with NOMAD CAMELS on lab computers. CAMELS opens an
outbound, encrypted WebSocket to the broker, so the lab computer needs no inbound
port or VPN. OASIS sends commands down that connection. The protocol is described
in [docs/camels_remote_protocol.md](../docs/camels_remote_protocol.md).

Status: pairing, the connection with heartbeat and reconnection, and read-only
operations such as `list_protocols` work. Computers are added and paired on the
CAMELS connections setup page. Starting and stopping measurements through the
broker and live plots follow in later phases.

## Endpoints

| Path | Reached by | Purpose |
|---|---|---|
| `POST <prefix>/pair` | CAMELS, through Nginx | Redeem a pairing code for a device token |
| `WS <prefix>/agent` | CAMELS, through Nginx | Agent connection |
| `/internal/...` | OASIS containers only | Device management and commands; needs `X-Broker-Secret` |
| `GET /healthz` | Docker network | Liveness |

`<prefix>` is `CAMELS_BROKER_PUBLIC_PREFIX`: `/backend/camels-remote` in the OASIS
stack, `/nomad-oasis/camels-remote` in the stand-alone test below. Nginx must route
only `<prefix>/`. The `/internal` API must never be routed, and it also requires
the broker secret.

## Local test with Docker

This runs the broker and an Nginx with TLS on port 8443, separate from the OASIS
stack. Run in Git Bash from `camels_broker/dev`:

```bash
# Once: secret and a self-signed certificate for the server's IP address
printf 'CAMELS_BROKER_SECRET=%s\n' "$(openssl rand -hex 32)" > .env
mkdir -p tls
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout tls/server.key -out tls/server.crt \
  -subj "/CN=10.188.162.32" -addext "subjectAltName=IP:10.188.162.32"

docker compose -p camels_broker_dev up -d --build
```

`.env` and `tls/` are ignored by Git. Copy only `tls/server.crt` to the lab computer,
never `server.key`.

Create a device and its pairing code (valid once, for 10 minutes):

```bash
docker compose -p camels_broker_dev exec camels_broker python -m camels_broker.admin create-device "Lab PC 1"
```

In CAMELS, open **Tools > OASIS Remote Control...**, and enter:

- OASIS address: `https://10.188.162.32:8443/nomad-oasis`
- CA certificate: the copied `server.crt`
- Pairing code: the code from the command above

After **Pair**, the dialog shows *Connected*. Then send commands:

```bash
ADMIN="docker compose -p camels_broker_dev exec camels_broker python -m camels_broker.admin"
$ADMIN list
$ADMIN command <device_id> list_protocols
$ADMIN command <device_id> get_protocol_schema --payload '{"protocol": "IV_Sweep"}'
$ADMIN delete <device_id>        # revokes the token; CAMELS shows "Rejected by OASIS"
```

Only protocols with **OASIS remote control** enabled in CAMELS are listed.

Stop and remove everything, including the device database:

```bash
docker compose -p camels_broker_dev down -v
```

## Tests

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests
```

## In the OASIS stack

`docker-compose.yaml` contains the `camels_broker` service, and
`configs/nginx_base_conf` routes `/backend/camels-remote/` to it. Old
`/nomad-oasis/camels-remote/` URLs are rewritten to it, so computers paired with
the former address keep working. Each OASIS
computer needs:

1. In `.env`:

   ```
   CAMELS_BROKER_SECRET=<output of: openssl rand -hex 32>
   ```

   The app and the broker share the secret.

   The setup page proposes the address to enter in CAMELS. The start script
   detects this computer's network address and passes it to the app as
   `CAMELS_REMOTE_DETECTED_HOST`; users can still change the address on the
   page. On a server with a fixed name, set it in `.env` instead, which takes
   precedence:

   ```
   CAMELS_REMOTE_PUBLIC_URL=https://oasis.example.org/backend
   ```

2. HTTPS, because CAMELS only connects with TLS. Either use
   `configs/nginx_https.conf` with a real certificate, or keep HTTP for browsers
   and add HTTPS for CAMELS with a local `docker-compose.override.yaml`
   (ignored by Git):

   ```yaml
   services:
     proxy:
       volumes:
         - ./configs/nginx_http_and_https.conf:/etc/nginx/conf.d/default.conf:ro
         - ./tls/selfsigned.crt:/etc/nginx/tls/mounted-nomad-oasis.crt:ro
         - ./tls/selfsigned.key:/etc/nginx/tls/mounted-nomad-oasis.key:ro
   ```

   A self-signed certificate is created as in the local test above, with
   `subjectAltName=IP:<address>`. Copy `selfsigned.crt` to the lab computers and
   select it as CA certificate in CAMELS.

3. Rebuild and start: `python nomad_camels_oasis/start_local_camels_dashboard.py`.

Then open the **CAMELS connections** dashboard, add a computer under
**Remote CAMELS computers**, and enter the shown address and pairing code in
CAMELS under **Tools > OASIS Remote Control...**.
