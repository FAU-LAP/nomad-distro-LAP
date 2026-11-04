# Migrating the LAP Oasis from NOMAD 1.3.17 to 2.0

This runbook moves the live Oasis (https://lap.nomad.data.fau.de) from its own
Docker Compose setup with NOMAD 1.3.17 to this repository with NOMAD 2.0. It
follows the official
[NOMAD Oasis Migration Guide](https://docs.nomad-lab.eu/howto/oasis/migrate.html).

The repository is used as it is. Settings that belong to the server go into
files that git ignores (`.env`, `.env.north`, `docker-compose.override.yaml`),
so later updates are a `git pull` and can run automatically
(see [Updates](#updates-after-the-migration)).

The old setup is not changed: its database and files are copied, not moved.
If 2.0 does not work, the [rollback](#rollback) is to start the old setup again.

> [!WARNING]
> `ghcr.io/fau-lap/nomad-distro-lap:main` is now NOMAD 2.0. Until step 1 is
> done, do not pull images for the old setup or recreate its NOMAD containers:
> they would start 2.0 on the old setup.

**Downtime:** from step 3 until step 4 is done.
**Needs:** bash, git, openssl and Docker with Compose v2, run by a user who may
use Docker. No `sudo` is needed: copies run inside containers.

The names that differ between servers are kept in `~/nomad-migration.env`.
Every block loads that file first, so the blocks also work in a new terminal.

## 1. Prepare (the Oasis keeps running)

List the running containers:

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}'
```

Write down where things are. Change the values to match the server:

```sh
cat > ~/nomad-migration.env <<'EOF'
OLD_DIR=/srv/nomad              # folder with the current docker-compose file
OLD_APP=nomad_oasis_app         # current NOMAD app container
OLD_MONGO=nomad_oasis_mongo     # current MongoDB container
NEW_DIR=/srv/nomad-distro-LAP   # new folder for this repository (must not exist yet)
BACKUP=/srv/nomad-backup        # folder for the database backup
EOF
```

Read the rest from the running containers:

```sh
. ~/nomad-migration.env
mount_of() { docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{if eq .Type \"volume\"}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}" "$1"; }
cat >> ~/nomad-migration.env <<EOF
OLD_FS='$(mount_of "$OLD_APP" /app/.volumes/fs)'
OLD_MONGO_DATA='$(mount_of "$OLD_MONGO" /data/db)'
OLD_MONGO_IMAGE='$(docker inspect -f '{{.Config.Image}}' "$OLD_MONGO")'
OLD_DB='$(docker exec "$OLD_APP" python -c 'from nomad.config import config; print(config.mongo.db_name)' 2>/dev/null | tail -n 1)'
EOF
cat ~/nomad-migration.env
```

Every line must have a value: `OLD_FS` is the folder (or volume) with the
uploaded files, `OLD_MONGO_DATA` the MongoDB data and `OLD_DB` the database
name. If one is empty, stop and ask.

Keep the old NOMAD image under its own name and check its version:

```sh
. ~/nomad-migration.env
docker tag "$(docker inspect -f '{{.Image}}' "$OLD_APP")" nomad-distro-lap:1.3-rollback
docker run --rm --entrypoint python nomad-distro-lap:1.3-rollback \
  -c "import importlib.metadata as m; print(m.version('nomad-lab'))" 2>/dev/null | tail -n 1
```

This must print `1.3.17`. Then, in the old compose file, set the `image` of
every NOMAD service (app, worker, north and so on) to
`nomad-distro-lap:1.3-rollback`. This changes nothing now, but it keeps the old
setup on 1.3 if it is ever started again. `docker compose config --images` in
`OLD_DIR` shows the result.

Check for name clashes, disk space and the kernel:

```sh
. ~/nomad-migration.env
for v in nomad_oasis_mongo nomad_oasis_elastic_9 nomad_oasis_postgresql nomad_oasis_north; do
  docker volume inspect "$v" >/dev/null 2>&1 && echo "exists: $v"
done
docker run --rm -v "$OLD_FS:/fs:ro" alpine:3.20 du -sh /fs
mkdir -p "$BACKUP" && df -h "$BACKUP" "$(dirname "$NEW_DIR")"
docker info --format '{{.KernelVersion}}'
```

- **Volumes:** the new setup creates these volumes. If it prints
  `exists: nomad_oasis_mongo` and that is `OLD_MONGO_DATA`, step 3 has one
  extra block. If it prints anything else, stop and ask.
- **Disk space:** the new folder needs room for a copy of the files (the size
  printed by `du`), and you need room for the database backup, the new
  database and search index, and about 10 GB of images.
- **Kernel:** MongoDB 8.0 does not start on Linux kernels 6.19 to 7.0.13
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). If the
  printed version is in that range, see step 2.

## 2. Set up the new folder (the Oasis keeps running)

```sh
. ~/nomad-migration.env
git clone https://github.com/FAU-LAP/nomad-distro-LAP.git "$NEW_DIR"
cd "$NEW_DIR"
bash scripts/generate-env.sh
chmod 600 .env .env.north
```

### Login

The Oasis logs users in with the FAU Keycloak, not the central NOMAD one.
Copy the login settings of the running Oasis into the new `.env` and
`.env.north`. This includes the Keycloak account NOMAD uses to look up users:

```sh
. ~/nomad-migration.env
cd "$NEW_DIR"
touch login.tmp && chmod 600 login.tmp
docker exec -i "$OLD_APP" python - <<'EOF' | grep -E '^(env|north) ' > login.tmp
from nomad.config import config
k = config.keycloak
central = config.oasis.uses_central_user_management
print(f"env NOMAD_OASIS_USES_CENTRAL_USER_MANAGEMENT={str(central).lower()}")
if not central:
    for name in ['server_url', 'public_server_url', 'realm_name', 'client_id', 'client_secret', 'username', 'password']:
        value = getattr(k, name)
        value = value.get_secret_value() if hasattr(value, 'get_secret_value') else value
        if value:
            print(f"env NOMAD_KEYCLOAK_{name.upper()}='{value}'")
    print(f'north KEYCLOAK_URL="{(k.public_server_url or k.server_url).rstrip("/")}"')
    print(f'north KEYCLOAK_REALM="{k.realm_name}"')
    print(f'north OAUTH_CLIENT_ID="{k.client_id}"')
    print(f'north OAUTH_CLIENT_SECRET="{k.client_secret or ""}"')
EOF
sed -n 's/^env //p' login.tmp >> .env
if grep -q '^north ' login.tmp; then
  sed -i -E '/^(KEYCLOAK_URL|KEYCLOAK_REALM|OAUTH_CLIENT_ID|OAUTH_CLIENT_SECRET)=/d' .env.north
  sed -n 's/^north //p' login.tmp >> .env.north
fi
rm login.tmp
grep -hE '^(NOMAD_OASIS|NOMAD_KEYCLOAK|KEYCLOAK|OAUTH)_' .env .env.north | sed -E 's/((PASSWORD|SECRET)=.).+(.)$/\1***\3/'
```

The output should show `NOMAD_OASIS_USES_CENTRAL_USER_MANAGEMENT=false`, the
FAU Keycloak (`keycloak-cdi.eln.data.fau.de`, realm `sso-cdi`, client
`nomad_lap`) and the account; passwords and secrets are shown as `***`. If it
shows `=true`, stop and ask.

Then look at the other settings of the old Oasis:

```sh
. ~/nomad-migration.env
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/nomad.yaml"}}nomad.yaml: {{.Source}}{{println}}{{end}}{{end}}{{range .Config.Env}}{{println .}}{{end}}' "$OLD_APP" | grep -E '^nomad.yaml|^NOMAD_'
```

The login is already copied. Hosts, ports, paths, the database and index
names and `api_secret` must not be copied. If anything else in that
`nomad.yaml` or those variables is still needed (for example an upload limit
or NORTH tools), tell us: it belongs in `configs/nomad.yaml` of this
repository, so it is in the image.

### Server settings

The server's own settings go into `docker-compose.override.yaml`. Docker
Compose reads this file automatically, and git ignores it. First find the
certificate and key the current proxy uses (put in the name of the proxy
container from step 1):

```sh
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' nomad_oasis_proxy
```

If a folder is mounted (for example `/etc/ssl/lap -> /etc/nginx/ssl`), the
files are inside it. Then write the file:

```sh
. ~/nomad-migration.env
cd "$NEW_DIR"
cat > docker-compose.override.yaml <<'EOF'
# Settings of the LAP server. Git ignores this file; docker compose reads it automatically.
services:
  north:
    environment:
      OAUTH_CALLBACK_URL: "https://lap.nomad.data.fau.de/backend/north/hub/oauth_callback"
  proxy:
    volumes:
      - ./configs/nginx_https.conf:/etc/nginx/conf.d/default.conf:ro
      - /path/to/certificate.crt:/etc/nginx/tls/mounted-nomad-oasis.crt:ro
      - /path/to/private.key:/etc/nginx/tls/mounted-nomad-oasis.key:ro
EOF
```

Replace the two `/path/to/...` with the certificate and key files. The HTTPS
config replaces the HTTP one of `docker-compose.yaml`. Only if the kernel from
step 1 is in the affected range, also add this to the file:

```yaml
  mongo:
    environment:
      - GLIBC_TUNABLES=glibc.pthread.rseq=1
```

Check the result and download the new images. This does not affect the old
setup:

```sh
. ~/nomad-migration.env
cd "$NEW_DIR"
docker compose config --quiet
docker compose config | grep OAUTH_CALLBACK_URL
docker compose config proxy | sed -n '/^  proxy:/,/^  [a-z]/p' | grep -E 'source:|target: /'
docker compose pull
```

This must show the `lap.nomad.data.fau.de` callback, `nginx_https.conf` (not
`nginx_http.conf`) as `default.conf`, and your two certificate files as
`mounted-nomad-oasis.crt` and `.key`.

## 3. Stop the old Oasis and copy the data (downtime starts)

Check that no upload is still processing. After some log lines, this must
say `Showing all 0 uploads selected`:

```sh
. ~/nomad-migration.env
docker exec "$OLD_APP" python -m nomad.cli admin uploads --processing ls
```

Stop the old Oasis and back up its database. MongoDB runs once more in a
temporary container for that:

```sh
. ~/nomad-migration.env
cd "$OLD_DIR" && docker compose down
docker run -d --name mongo-backup -v "$OLD_MONGO_DATA:/data/db" "$OLD_MONGO_IMAGE"
shell=$(docker exec mongo-backup sh -c 'command -v mongo || command -v mongosh')
until docker exec mongo-backup "$shell" --quiet --eval 'db.runCommand({ping: 1}).ok' >/dev/null 2>&1; do sleep 2; done
docker exec mongo-backup mongodump -d "$OLD_DB" --archive --gzip > "$BACKUP/$OLD_DB.archive.gz"
docker exec mongo-backup "$shell" --quiet "$OLD_DB" --eval 'db.upload.countDocuments({}) + " uploads, " + db.entry.countDocuments({}) + " entries"'
docker rm -fv mongo-backup
```

Note the numbers of uploads and entries. If `docker compose down` says the
network still has active endpoints, stop the NORTH tool containers that are
still running (`docker ps`) and run it again.

Copy the files into the new folder. The old files are only read:

```sh
. ~/nomad-migration.env
docker run --rm -v "$OLD_FS:/from:ro" -v "$NEW_DIR/.volumes/fs:/to" alpine:3.20 \
  sh -c 'cp -a /from/. /to/ && chown -R 1000 /to'
```

**Only if step 1 printed `exists: nomad_oasis_mongo`:** the new MongoDB needs
that volume name, so copy the old database to `nomad_oasis_mongo_1.3`:

```sh
. ~/nomad-migration.env
docker volume create nomad_oasis_mongo_1.3
docker run --rm -v nomad_oasis_mongo:/from:ro -v nomad_oasis_mongo_1.3:/to alpine:3.20 cp -a /from/. /to/
docker volume rm nomad_oasis_mongo
sed -i 's/^OLD_MONGO_DATA=.*/OLD_MONGO_DATA=nomad_oasis_mongo_1.3/' ~/nomad-migration.env
```

For the rollback, also set the name of the MongoDB volume in the old compose
file to `nomad_oasis_mongo_1.3`. When the old setup starts with it, Compose
warns that the volume "was not created by Docker Compose"; that is expected.

## 4. Start NOMAD 2.0

Start MongoDB 8 and load the backup into it:

```sh
. ~/nomad-migration.env
cd "$NEW_DIR"
docker compose up -d --wait mongo
docker exec -i nomad_oasis_mongo mongorestore --archive --gzip \
  --nsFrom "$OLD_DB.*" --nsTo 'nomad_oasis_v1.*' < "$BACKUP/$OLD_DB.archive.gz"
docker exec nomad_oasis_mongo mongosh --quiet nomad_oasis_v1 \
  --eval 'db.upload.countDocuments() + " uploads, " + db.entry.countDocuments() + " entries"'
```

`mongorestore` prints many lines; the last one must say
`0 document(s) failed to restore`. The numbers after it must be the same as in
step 3. Then start everything and rebuild the search index:

```sh
. ~/nomad-migration.env
cd "$NEW_DIR"
docker compose up -d
docker compose ps
docker compose exec app python -m nomad.cli admin uploads index --parallel 4
```

All services should be `healthy` or `running`. The `temporal-setup-db` and
`temporal-create-namespace` jobs run once and then show as exited; that is
expected. On this first start, one worker may restart once with
`resource_already_exists_exception` in its log; that is harmless. Search is
empty until the index command finishes.

## 5. Check

- https://lap.nomad.data.fau.de/backend/alive answers
- https://lap.nomad.data.fau.de/ shows the new GUI, `/gui/` the classic GUI
- Log in with an FAU account; your uploads and entries are there and searchable
- NORTH: start a Jupyter tool; files from before are in the home folder
- CAMELS: create a new app token in NOMAD and enter it in CAMELS (old tokens
  stop working, because the new Oasis has its own API secret)
- `docker compose logs app worker` shows no errors

## Rollback

Use this only if 2.0 does not work. Changes made in 2.0 are lost.

```sh
. ~/nomad-migration.env
cd "$NEW_DIR" && docker compose down
cd "$OLD_DIR" && docker compose up -d
```

The old database and files were not changed, so 1.3 starts as before. This
needs the image change from step 1, and the volume name from step 3 if you
used that block.

## Updates after the migration

`scripts/update.sh` brings the Oasis to the newest version of this
repository: it runs `git pull`, pulls the images and restarts what changed.
To deploy every night at 3:00, add this with `crontab -e` (change the paths):

```
0 3 * * * flock -n /tmp/nomad-update.lock bash /srv/nomad-distro-LAP/scripts/update.sh >> /srv/nomad-update.log 2>&1
```

Everything merged into `main` is built and tested by CI and goes live the
following night. Changes to the LAP plugins and NOMAD patch releases (2.0.1 to
2.0.2) are installed automatically. A new NOMAD minor or major version (2.0 to
2.1) can need migration steps, so the script does not install it: it writes a
message to the log and changes nothing.

The server settings stay in place: git ignores `.env`, `.env.north` and
`docker-compose.override.yaml`, so `git pull` does not change them, and Docker
Compose reads the override file automatically. There is no need for
`git reset` or a script that adds the settings again. Do not edit files that
git tracks: if an update changes the same file, `git pull` stops and the
update is skipped.

## Clean up (after a few weeks without problems)

- the old Docker volumes: `OLD_MONGO_DATA`, and the Elasticsearch and RabbitMQ
  volumes from the old compose file (`docker volume rm ...`)
- the old files `OLD_FS`
- `docker image rm nomad-distro-lap:1.3-rollback`
- `~/nomad-migration.env`, and the backup in `BACKUP` when your backup policy
  allows it

## Tested

The test ran on 2026-11-04 in an isolated Docker engine. The old Oasis there
was a self-made Docker Compose setup with NOMAD 1.3.17, the image the server
runs. It used the same container, volume and network names as the NOMAD
template, but kept its files in a folder outside the setup and used a
different database name. It ran HTTPS, and it held published and unpublished
test uploads with linked LAP entries, raw files and a NORTH home folder. The
blocks of this document ran unchanged; only the values in
`~/nomad-migration.env`, the image and volume names in the old compose file,
and the certificate paths were filled in, as described above.

- Step 1 found the file folder, database name and MongoDB volume by itself,
  and reported the clash of the old `nomad_oasis_mongo` volume.
- After step 2, `git status` in the new folder was clean: all server settings
  were in ignored files.
- The database went from MongoDB 5.0.6 to 8.0 by backup and restore, and was
  renamed to `nomad_oasis_v1` on the way. All services became healthy.
- The data was identical to before: the same uploads, published state, raw
  files and entry contents (including references), every entry in the new
  search index, and the NORTH home folder. Reprocessing in 2.0 worked without
  errors or warnings.
- HTTPS with the old certificate, the new and the classic GUI, NORTH and the
  old `/nomad-oasis/api` path (used by CAMELS) worked. A token from 1.3 was
  rejected, as expected.
- The login block copied FAU-style settings (with a dummy account) into
  `.env` and `.env.north`, special characters in the password included. NOMAD,
  both GUIs and the NORTH login then pointed to the FAU Keycloak.
- `scripts/update.sh` from cron changed nothing when there was nothing new,
  and refused a NOMAD version jump without changing anything.
- The rollback started 1.3.17 again with identical data; 1.3 tokens worked
  again and the old files were unchanged.

Not tested, because it needs the real server, its data or the FAU Keycloak:
the server's actual setup, real uploads (for example CAMELS data), logging in
through the FAU Keycloak, looking up users there with the real account, and
starting a NORTH tool.
