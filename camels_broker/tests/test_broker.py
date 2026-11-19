import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from camels_broker.app import SUBPROTOCOL, Settings, create_app

SECRET = "s" * 40
PREFIX = "/nomad-oasis/camels-remote"


@pytest.fixture
def client(tmp_path):
    settings = Settings(secret=SECRET, db_path=str(tmp_path / "broker.db"), command_timeout_seconds=2)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def internal(client, method, path, **kwargs):
    return client.request(method, "/internal" + path, headers={"X-Broker-Secret": SECRET}, **kwargs)


def paired_device(client, name="Lab PC"):
    created = internal(client, "POST", "/devices", json={"name": name, "owner": "user-1"}).json()
    response = client.post(PREFIX + "/pair", json={"pairing_code": created["pairing_code"].lower(), "hostname": "lab-pc"})
    assert response.status_code == 200, response.text
    return response.json()


def connect(client, token, subprotocols=(SUBPROTOCOL,)):
    return client.websocket_connect(
        PREFIX + "/agent",
        headers={"Authorization": f"Bearer {token}"},
        subprotocols=list(subprotocols),
    )


def hello(ws):
    ws.send_text(json.dumps({"version": 1, "type": "hello", "id": "h1", "hostname": "lab-pc",
                             "camels_version": "test", "operations": ["list_protocols"]}))
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome


def test_internal_api_requires_secret(client):
    assert client.get("/internal/devices").status_code == 401
    assert client.get("/internal/devices", headers={"X-Broker-Secret": "wrong"}).status_code == 401
    assert internal(client, "GET", "/devices").status_code == 200


def test_pairing_code_is_single_use(client):
    created = internal(client, "POST", "/devices", json={"name": "Lab PC"}).json()
    code = created["pairing_code"]
    assert client.post(PREFIX + "/pair", json={"pairing_code": code}).status_code == 200
    assert client.post(PREFIX + "/pair", json={"pairing_code": code}).status_code == 400


def test_expired_pairing_code_is_rejected(client):
    created = internal(client, "POST", "/devices", json={"name": "Lab PC"}).json()
    store = client.app.state.store
    with store._conn:
        store._conn.execute("UPDATE pairing_codes SET expires_at = 0")
    assert client.post(PREFIX + "/pair", json={"pairing_code": created["pairing_code"]}).status_code == 400


def test_token_is_stored_only_as_hash(client):
    device = paired_device(client)
    rows = client.app.state.store._conn.execute("SELECT * FROM devices").fetchall()
    assert device["device_token"] not in json.dumps([dict(row) for row in rows])


def test_invalid_token_is_rejected_with_401(client):
    from starlette.testclient import WebSocketDenialResponse

    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with connect(client, "wrong-token"):
            pass
    assert excinfo.value.status_code == 401


def test_wrong_protocol_version_closes_with_4426(client):
    device = paired_device(client)
    with connect(client, device["device_token"], subprotocols=["camels-remote.v0"]) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()
    assert excinfo.value.code == 4426


def test_command_round_trip_and_state(client):
    device = paired_device(client)
    device_id = device["device_id"]
    assert internal(client, "GET", f"/devices/{device_id}/state").json()["online"] is False

    with connect(client, device["device_token"]) as ws:
        hello(ws)
        state = internal(client, "GET", f"/devices/{device_id}/state").json()
        assert state["online"] is True
        assert state["agent"]["operations"] == ["list_protocols"]

        result = {}

        def call():
            result.update(internal(client, "POST", f"/devices/{device_id}/commands",
                                   json={"operation": "list_protocols", "requested_by": "user-1"}).json())

        thread = threading.Thread(target=call)
        thread.start()
        command = ws.receive_json()
        assert command["type"] == "command"
        assert command["operation"] == "list_protocols"
        assert command["requested_by"] == "user-1"
        ws.send_text(json.dumps({"version": 1, "type": "command_result", "id": "r1",
                                 "command_id": command["command_id"], "status": "completed",
                                 "result": {"Protocols": ["IV"]}}))
        thread.join(5)
        assert result == {"status": "completed", "result": {"Protocols": ["IV"]}}

        ws.send_text(json.dumps({"version": 1, "type": "status", "id": "s1", "state": "running", "run_id": "r-1"}))
        time.sleep(0.2)
        assert internal(client, "GET", f"/devices/{device_id}/state").json()["measurement"]["state"] == "running"

    time.sleep(0.2)
    assert internal(client, "GET", f"/devices/{device_id}/state").json()["online"] is False


def test_offline_device_gets_device_offline_immediately(client):
    device = paired_device(client)
    start = time.monotonic()
    result = internal(client, "POST", f"/devices/{device['device_id']}/commands",
                      json={"operation": "list_protocols"}).json()
    assert time.monotonic() - start < 1
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "device_offline"


def test_unanswered_command_times_out_as_unknown(client):
    device = paired_device(client)
    with connect(client, device["device_token"]) as ws:
        hello(ws)
        result = internal(client, "POST", f"/devices/{device['device_id']}/commands",
                          json={"operation": "list_protocols", "timeout_seconds": 0.5}).json()
        assert ws.receive_json()["type"] == "command"
    assert result["status"] == "unknown"
    assert result["error"]["code"] == "timeout"


def test_newer_connection_replaces_older(client):
    device = paired_device(client)
    with connect(client, device["device_token"]) as first:
        hello(first)
        with connect(client, device["device_token"]) as second:
            hello(second)
            with pytest.raises(WebSocketDisconnect) as excinfo:
                first.receive_text()
            assert excinfo.value.code == 4409


def test_deleting_device_revokes_connection_and_token(client):
    device = paired_device(client)
    with connect(client, device["device_token"]) as ws:
        hello(ws)
        assert internal(client, "DELETE", f"/devices/{device['device_id']}").status_code == 200
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()
        assert excinfo.value.code == 4401
    with pytest.raises(WebSocketDisconnect):
        with connect(client, device["device_token"]):
            pass


def test_pairing_again_revokes_old_token(client):
    device = paired_device(client)
    code = internal(client, "POST", f"/devices/{device['device_id']}/pairing-code").json()["pairing_code"]
    new = client.post(PREFIX + "/pair", json={"pairing_code": code}).json()
    assert new["device_id"] == device["device_id"]
    assert new["device_token"] != device["device_token"]
    with pytest.raises(WebSocketDisconnect):
        with connect(client, device["device_token"]):
            pass


def test_changes_long_poll(client):
    device = paired_device(client)
    device_id = device["device_id"]
    changes = f"/devices/{device_id}/changes"
    with connect(client, device["device_token"]) as ws:
        ws.send_text(json.dumps({"version": 1, "type": "hello", "id": "h1", "hostname": "lab-pc",
                                 "camels_version": "test", "operations": [], "features": ["changes"]}))
        assert ws.receive_json()["type"] == "welcome"
        first = internal(client, "GET", changes + "?timeout=0").json()
        assert first["online"] is True and first["push"] is True

        # Nothing changes: the request waits for the timeout.
        start = time.monotonic()
        same = internal(client, "GET", changes + f"?after={first['seq']}&timeout=0.5").json()
        assert same["seq"] == first["seq"] and time.monotonic() - start >= 0.45

        # A change reported by the agent ends the wait at once.
        result = {}

        def wait():
            start = time.monotonic()
            result.update(internal(client, "GET", changes + f"?after={first['seq']}&timeout=10").json())
            result["seconds"] = time.monotonic() - start

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.3)
        ws.send_text(json.dumps({"version": 1, "type": "changed", "id": "c1"}))
        thread.join(5)
        assert result["seq"] == first["seq"] + 1 and result["seconds"] < 2

    # Disconnecting is a change, too; an offline device does not make callers wait.
    time.sleep(0.2)
    start = time.monotonic()
    offline = internal(client, "GET", changes + f"?after={result['seq'] + 1}&timeout=10").json()
    assert offline["online"] is False and offline["push"] is False and time.monotonic() - start < 1


def test_changes_without_push_feature_do_not_wait(client):
    device = paired_device(client)
    with connect(client, device["device_token"]) as ws:
        hello(ws)
        start = time.monotonic()
        reply = internal(client, "GET", f"/devices/{device['device_id']}/changes?after=0&timeout=10").json()
        assert reply["push"] is False and time.monotonic() - start < 1
