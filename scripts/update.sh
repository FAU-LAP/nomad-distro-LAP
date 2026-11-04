#!/bin/bash

# Script to update the Oasis to the newest repository files and images.
# It can run unattended, for example every night from cron (see MIGRATION-2.0.md).
# A new NOMAD minor or major version is not installed, because it can need migration steps.

set -euo pipefail

# Go to the repository folder
cd "$(dirname "$0")/.."

nomad_version() {
    docker run --rm --entrypoint python "$1" \
        -c "import importlib.metadata as m; print(m.version('nomad-lab'))" 2>/dev/null | tail -n 1
}

echo "== $(date '+%F %T') update"

image=$(docker inspect -f '{{.Config.Image}}' nomad_oasis_app)
old_id=$(docker inspect -f '{{.Image}}' nomad_oasis_app)
docker pull --quiet "$image" >/dev/null

old_version=$(nomad_version "$old_id")
new_version=$(nomad_version "$image")
if [[ -z "$new_version" || "${old_version%.*}" != "${new_version%.*}" ]]; then
    # Put the tag back on the running image, so nothing changes
    docker tag "$old_id" "$image"
    echo "NOMAD $old_version -> ${new_version:-unknown} needs a manual update, see the release notes."
    echo "Nothing was changed."
    exit 1
fi

git pull --ff-only --quiet
docker compose --progress quiet pull
docker compose --progress quiet up -d --remove-orphans
docker image prune -f >/dev/null

echo "✓ Running NOMAD $new_version, repository at $(git rev-parse --short HEAD)"
