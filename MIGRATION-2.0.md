# Migrating the LAP Oasis from NOMAD 1.3.17 to 2.0

This runbook moves the live Oasis (https://lap.nomad.data.fau.de) from NOMAD
1.3.17 to 2.0. It follows the official
[NOMAD Oasis Migration Guide](https://docs.nomad-lab.eu/howto/oasis/migrate.html)
and adds the steps this server needs. Every command was tested on a copy of
the current 1.3.17 setup (see [Tested](#tested)).

> [!WARNING]
> Since PR #46, the `ghcr.io/fau-lap/nomad-distro-lap:main` image is NOMAD 2.0.
> Until this migration is done, do **not** run `docker compose pull`,
> `clean_up_pull_start_new.ps1` or anything else that pulls images on the
> server. The old setup would start the 2.0 image and break the Oasis.

**Downtime:** the Oasis is offline from step 2 to step 6.
**Run all commands** in a bash shell (on Windows: WSL) in the folder of the
repository checkout on the server. You need `git`, `python3` (3.8 or newer),
`curl` and `openssl`, and permission to use Docker.

## 1. Prepare (the Oasis keeps running)

```sh
git rev-parse HEAD > ../server-commit-before.txt   # for the rollback
git status                                         # note all local changes
git diff > ../server-changes.patch                 # keep a copy of them
cp docker-compose.yaml ../docker-compose.yaml.1.3
docker info --format '{{.KernelVersion}}'
du -sh .volumes/fs && df -h .
```

- **Disk space:** you need free space for about twice the size of
  `.volumes/fs` (backup and file restore), four MongoDB dumps, the new search
  index and about 10 GB for the new images.
- **Kernel:** MongoDB 8.0 does not start on Linux kernels 6.19 to 7.0.13
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). If the
  printed version is in that range, add `- GLIBC_TUNABLES=glibc.pthread.rseq=1`
  to the `environment` of the `mongo` service in both `mongo-upgrade.yaml`
  (step 4) and `docker-compose.yaml` (step 5).
- **HTTPS:** look at `../server-changes.patch` and note which certificate and
  key files the `proxy` service mounts. You need them again in step 5.

Keep the old 1.3 image under its own name for the rollback, and check it:

```sh
docker tag ghcr.io/fau-lap/nomad-distro-lap:main nomad-distro-lap:1.3-rollback
docker run --rm --entrypoint python nomad-distro-lap:1.3-rollback \
  -c "import importlib.metadata as m; print(m.version('nomad-lab'))"
```

This must print `1.3.17`. If it prints `2.0.x`, someone has already pulled the
new image; stop and ask before continuing.

## 2. Stop the Oasis

Check that no upload is still processing (this must say `0 uploads selected`),
then stop everything:

```sh
docker compose exec app python -m nomad.cli admin uploads --processing ls
docker compose down
```

## 3. Back up

```sh
docker compose up -d mongo
docker exec nomad_oasis_mongo mongodump -d nomad_oasis_v1 -o /backup
docker compose down
tar -czf ../fs-backup-1.3.tar.gz .volumes/fs
```

Use `sudo` for `tar` if your user cannot read all files in `.volumes/fs`.

The MongoDB dump is written to `.volumes/mongo/nomad_oasis_v1`. Copy it and
`../fs-backup-1.3.tar.gz` to a second location before you continue.

## 4. Upgrade MongoDB 5 → 8

MongoDB has to go through 6.0 and 7.0. FAIRmat's `upgrade_mongo.py` does
this, but it restarts and pulls **every** service in the compose file it is
given. Give it a file with only MongoDB, so it does not touch NOMAD:

```sh
cat > mongo-upgrade.yaml <<'EOF'
# Temporary: only the mongo service from the old docker-compose.yaml,
# so that upgrade_mongo.py restarts nothing else. Delete after the upgrade.
services:
  mongo:
    image: docker.io/mongo:5.0.6
    container_name: nomad_oasis_mongo
    environment:
      - MONGO_DATA_DIR=/data/db
      - MONGO_LOG_DIR=/dev/null
    volumes:
      - mongo:/data/db
      - ./.volumes/mongo:/backup
    command: mongod --logpath=/dev/null
    healthcheck:
      test:
        - "CMD"
        - "mongo"
        - "mongo:27017/test"
        - "--quiet"
        - "--eval"
        - "'db.runCommand({ping:1}).ok'"
      interval: 10s
      timeout: 10s
      retries: 30
      start_period: 10s

volumes:
  mongo:
    name: "nomad_oasis_mongo"

networks:
  default:
    name: nomad_oasis_network
EOF

curl -O https://gitlab.mpcdf.mpg.de/nomad-lab/nomad-FAIR/-/snippets/188/raw/main/upgrade_mongo.py
docker compose -f mongo-upgrade.yaml up -d --wait
python3 upgrade_mongo.py -c nomad_oasis_mongo -f mongo-upgrade.yaml --from-version 5.0.6
```

Answer `y`. The script needs MongoDB running when it starts, hence the
`up -d --wait`. It ends with `MongoDB is now running version 8.0`. Check:

```sh
docker exec nomad_oasis_mongo mongosh --quiet --eval 'db.version()'
docker exec nomad_oasis_mongo mongosh --quiet nomad_oasis_v1 \
  --eval 'db.upload.countDocuments() + " uploads, " + db.entry.countDocuments() + " entries"'
```

Then clean up. The script leaves a backup per stage in the repository folder;
move them out:

```sh
docker compose -f mongo-upgrade.yaml down
mkdir -p ../mongo-upgrade-backups && mv mongo_backup_pre_* ../mongo-upgrade-backups/
rm mongo-upgrade.yaml upgrade_mongo.py
```

## 5. Update the repository and configuration

```sh
git stash        # puts the local changes aside, they stay in ../server-changes.patch too
git pull
bash scripts/generate-env.sh
chmod 600 .env .env.north
```

Run `generate-env.sh` only once: running it again creates new secrets.

Then edit `docker-compose.yaml`:

1. In the `north` service, set
   `OAUTH_CALLBACK_URL: "https://lap.nomad.data.fau.de/backend/north/hub/oauth_callback"`.
2. In the `proxy` service, switch to HTTPS. Comment out the `nginx_http.conf`
   line, uncomment the `nginx_https.conf` line, and mount the **same**
   certificate and key as before (from step 1), but at the new paths
   `/etc/nginx/tls/mounted-nomad-oasis.crt` and `.key`. For example, if the
   old setup mounted `./ssl:/etc/nginx/ssl:ro`:

   ```yaml
         # HTTP
         # - ./configs/nginx_http.conf:/etc/nginx/conf.d/default.conf:ro
         # HTTPS (you need to have a TLS certificate)
         - ./configs/nginx_https.conf:/etc/nginx/conf.d/default.conf:ro
         - ./ssl/selfsigned.crt:/etc/nginx/tls/mounted-nomad-oasis.crt:ro
         - ./ssl/selfsigned.key:/etc/nginx/tls/mounted-nomad-oasis.key:ro
   ```

   The old paths (`/etc/nginx/ssl/...`) no longer work.
3. Re-apply any other local change from `../server-changes.patch` that is
   still needed. Do not re-apply old image names or the old MongoDB or
   Elasticsearch settings.

Check the file with `docker compose config --quiet` (no output means valid).

## 6. Start NOMAD 2.0 and rebuild the search index

```sh
docker compose pull
docker compose up -d
docker compose ps
```

All services should be `healthy` or `running`. The `temporal-setup-db` and
`temporal-create-namespace` jobs run once and then show as exited; that is
expected. On this first start, one worker may restart once with
`resource_already_exists_exception` in its log: the app and the workers all
try to create the new, empty search index at the same time. That is harmless.

Then rebuild the search index from MongoDB and the files:

```sh
docker compose exec app python -m nomad.cli admin uploads index --parallel 4
```

Search is empty until this finishes. The old Elasticsearch 7 data stays in
the `nomad_oasis_elastic` volume as a rollback.

## 7. Check

- https://lap.nomad.data.fau.de/backend/alive answers
- https://lap.nomad.data.fau.de/ shows the new GUI, `/gui/` the classic GUI
- Log in; your uploads and entries are there and searchable
- NORTH: start a Jupyter tool
- CAMELS: create a new app token in NOMAD and enter it in CAMELS (old tokens
  stop working, because the Oasis now has its own API secret)
- `docker compose logs app worker` shows no errors

## Rollback

Use this only if 2.0 does not work. Changes made in 2.0 after the migration
are lost.

```sh
docker compose down
git reset --hard $(cat ../server-commit-before.txt)
git stash pop
```

In `docker-compose.yaml`, replace every `ghcr.io/fau-lap/nomad-distro-lap:main`
with `nomad-distro-lap:1.3-rollback`, and set the name of the `mongo` volume
(at the bottom) to `nomad_oasis_mongo_rollback`.

Then restore the backups from step 3. NOMAD 2.0 writes archive files in a
format that 1.3 cannot read, so the files are restored too. MongoDB 5 cannot
read the upgraded database, so the dump goes into a new, empty volume:

```sh
mv .volumes/fs ../fs-after-2.0
tar -xzf ../fs-backup-1.3.tar.gz
sudo chown -R 1000 .volumes/fs
docker compose up -d mongo
docker exec nomad_oasis_mongo mongorestore -d nomad_oasis_v1 /backup/nomad_oasis_v1
docker compose up -d
```

The Elasticsearch 7 volume was not changed, so search works right away.
Tokens created in 1.3 work again.

## After the migration

`clean_up_pull_start_new.ps1` only pulls images. When `docker-compose.yaml`
or other files in this repository change, also run `git pull` before it.

## Clean up (after a few weeks without problems)

```sh
git stash drop
docker volume rm nomad_oasis_elastic nomad_oasis_rabbitmq
docker image rm nomad-distro-lap:1.3-rollback
```

Keep the backups from steps 3 and 4 for as long as your backup policy says.

## Tested

On 2026-11-04 the whole runbook, including the rollback, was run twice on a
copy of the server setup in an isolated Docker engine: the old `5a1c4a7`
checkout with HTTPS switched on, the NOMAD 1.3.17 image the server runs, and
published and unpublished test uploads with linked LAP entries and raw files.
The second run used the command blocks of this document unchanged. The 2.0
images were pulled from the public registries.

- MongoDB went 5.0.6 → 6.0 → 7.0 → 8.0 (feature compatibility 8.0) with all
  uploads and entries.
- After step 6 all services were healthy and the data was identical to
  before: the same uploads, published state, raw files and entry contents
  (including references), and every entry in the new search index.
- Reprocessing the migrated uploads in 2.0 worked without errors.
- HTTPS, the new GUI, the classic GUI, NORTH and the old `/nomad-oasis/api`
  path (used by CAMELS) worked. A token from 1.3 was rejected, as expected.
- The rollback, done after 2.0 had reprocessed the uploads, restored 1.3.17
  with identical data; entries opened again and 1.3 tokens worked again.

Not tested, because it needs the real server or its data: the server's exact
local changes, certificate setup and operating system, the real uploads (for
example CAMELS data), logging in through the NOMAD Keycloak, and starting a
NORTH tool.
