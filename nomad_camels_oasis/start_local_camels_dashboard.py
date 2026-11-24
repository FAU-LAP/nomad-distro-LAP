"""Start the local classic OASIS instance with the CAMELS plugin.

Run from the repository root:

    python nomad_camels_oasis/start_local_camels_dashboard.py

Docker Desktop must be running. The script builds the local plugin image (using
Docker's cache when unchanged), then starts the single project compose stack.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess


ROOT = Path(__file__).resolve().parent.parent
# Local tags; docker-compose.yaml uses them through APP_IMAGE and
# NOMAD_CPU_WORKER_IMAGE instead of the published images.
IMAGE = "nomad-distro-lap:local"
# The Temporal worker that runs the CAMELS measurement Actions.
CPU_ACTION_IMAGE = "nomad-distro-lap/cpu-action:local"


def main() -> None:
    try:
        subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise SystemExit(
            "Docker Desktop is not accessible. Start Docker Desktop and ensure "
            "this Windows user can run 'docker info'."
        )

    print("Building the local OASIS image with the CAMELS and GUI v2 plugins ...")
    subprocess.run(
        ["docker", "build", "--target", "final", "-t", IMAGE, "."],
        cwd=ROOT,
        check=True,
    )
    # Same code for the worker, so Actions use the current plugin as well.
    print("Building the CPU Action worker image ...")
    subprocess.run(
        ["docker", "build", "--target", "cpu_action_final", "-t", CPU_ACTION_IMAGE, "."],
        cwd=ROOT,
        check=True,
    )

    print("Starting classic OASIS ...")
    environment = dict(os.environ)
    environment["APP_IMAGE"] = IMAGE
    environment["NOMAD_CPU_WORKER_IMAGE"] = CPU_ACTION_IMAGE
    host = detect_host_address()
    if host:
        environment["CAMELS_REMOTE_DETECTED_HOST"] = host
    # The broker has its own image; build it too, so code changes take effect.
    subprocess.run(["docker", "compose", "build", "camels_broker"], cwd=ROOT, env=environment, check=True)
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, env=environment, check=True)
    # Nginx resolves container addresses only at start. A recreated container
    # (for example the broker) gets a new address, so reload Nginx; otherwise
    # it answers 502 for the old address.
    subprocess.run(["docker", "compose", "exec", "-T", "proxy", "nginx", "-s", "reload"], cwd=ROOT, env=environment, check=False)
    print("\nReady:")
    print("  GUI:          http://localhost/")
    print("  CAMELS setup: http://localhost/backend/camels/setup")
    if host:
        print(f"  Address for CAMELS (detected): https://{host}/backend")


def detect_host_address() -> str | None:
    """Return this computer's IPv4 address on its main network.

    Docker containers cannot see the host's network adapters, so the address is
    detected here and passed to the app. Connecting a UDP socket sends no data;
    it only selects the adapter that would be used for outside traffic.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


if __name__ == "__main__":
    main()
