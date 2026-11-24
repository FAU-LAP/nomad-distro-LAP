# CAMELS remote control: local startup

How to run this OASIS with the CAMELS remote control plugin
(`nomad_camels_oasis`) on one Windows computer, for development and tests.

1. Start Docker Desktop and wait until it is running.

2. Once: create `.env` and `.env.north` with `scripts/generate-env.sh`, then add
   the broker secret to `.env` (see [camels_broker/README.md](../camels_broker/README.md)):

   ```
   CAMELS_BROKER_SECRET=<output of: openssl rand -hex 32>
   ```

3. Start NOMAD CAMELS on this computer. In CAMELS **Settings > API**:
   - enable the API,
   - set the API port to `8000`,
   - generate and copy an API key.

4. Open the protocol that OASIS should control. In **Protocol > General
   Settings**, enable *Expose this protocol to OASIS remote control* and save the
   protocol.

5. In the repository root, open PowerShell and run:

   ```powershell
   .\.venv\Scripts\python.exe .\nomad_camels_oasis\start_local_camels_dashboard.py
   ```

   The script builds the local app and CPU Action worker images, builds the
   broker, and starts Docker Compose with these images.

6. Open OASIS in a browser and sign in: <http://localhost/>

7. Open the **CAMELS setup** dashboard (or <http://localhost/backend/camels/setup>).
   - CAMELS on this computer: save a direct API connection with
     CAMELS API URL `http://host.docker.internal:8000` and the API key from
     step 3. Select *Test connection*, then *Save connection*.
   - CAMELS on another computer: add it under *Remote CAMELS computers* and
     enter the shown address and pairing code in CAMELS under
     **Tools > OASIS Remote Control...**. This needs HTTPS, see
     [camels_broker/README.md](../camels_broker/README.md).

8. Open the **CAMELS measurements** dashboard. Exposed protocols appear in the
   protocol list. Select a protocol, review the parameters, then start the
   measurement.

## Notes

- `host.docker.internal` is correct when OASIS runs in Docker Desktop and CAMELS
  runs on the same Windows computer.
- A protocol without *Expose this protocol to OASIS remote control* is not
  visible in OASIS.
- If the connection test fails, first check that CAMELS is still running and
  that its API is enabled on port 8000.
- URLs below the former base path `/nomad-oasis/` still work: Nginx redirects
  or rewrites them to `/` (GUI) and `/backend/` (backend, including the broker).
