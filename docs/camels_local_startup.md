OASIS -- CAMELS local startup guide
===================================

1. Start Docker Desktop and wait until it is running.

2. Start NOMAD CAMELS on this computer.
   In CAMELS Settings > API:
   - Enable API
   - Set API Port to 8000
   - Generate and copy an API key

3. Open the protocol that OASIS should control.
   In Protocol > General Settings, enable:
   "Expose this protocol to OASIS remote control".
   Save the protocol.

4. In the OASIS project root, open PowerShell and run:

   .\.venv\Scripts\python.exe .\nomad_camels_oasis\start_local_camels_dashboard.py

   The script builds the local OASIS image and starts Docker Compose.

5. Open OASIS in a browser and sign in:

   http://localhost/nomad-oasis/gui/v2/

6. Open the CAMELS connections setup page. Save a connection with:

   CAMELS API URL: http://host.docker.internal:8000
   CAMELS API key: <the key copied from CAMELS Settings > API>

   Select Test connection, then Save connection.

7. Open the CAMELS measurement dashboard. Exposed protocols should appear in
   the protocol list. Select a protocol, review or save parameters, then use
   Start measurement.

Notes
-----
- host.docker.internal is correct when OASIS runs in Docker Desktop and CAMELS
  runs on the same Windows computer.
- A protocol not marked "Expose this protocol to OASIS remote control" will
  not be visible in OASIS.
- If the connection test fails, first check that CAMELS is still running and
  that its API is enabled on port 8000.
