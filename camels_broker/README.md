# CAMELS remote broker

The broker connects OASIS with NOMAD CAMELS on lab computers. CAMELS opens an
outbound, encrypted WebSocket to the broker, so the lab computer needs no inbound
port or VPN. OASIS sends commands down that connection. The protocol is described
in [docs/camels_remote_protocol.md](../docs/camels_remote_protocol.md).

Status: phase P1. Pairing, the connection with heartbeat and reconnection, and
read-only operations such as `list_protocols` work. Starting and stopping
measurements, live plots and the OASIS setup page follow in later phases. Until
then, `camels_broker.admin` replaces the setup page.

## Endpoints

| Path | Reached by | Purpose |
|---|---|---|
| `POST /nomad-oasis/camels-remote/pair` | CAMELS, through Nginx | Redeem a pairing code for a device token |
| `WS /nomad-oasis/camels-remote/agent` | CAMELS, through Nginx | Agent connection |
| `/internal/...` | OASIS containers only | Device management and commands; needs `X-Broker-Secret` |
| `GET /healthz` | Docker network | Liveness |

Nginx must route only `/nomad-oasis/camels-remote/`. The `/internal` API must never
be routed, and it also requires the broker secret.

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

## Integration into the OASIS stack (phase P2)

Add the service to `docker-compose.yaml` and the location block from
`dev/nginx.conf` to `configs/nginx_base_conf`, with `CAMELS_BROKER_SECRET` in `.env`.
The plugin API and the Temporal activities then call the internal API at
`http://camels_broker:8000/internal`.
