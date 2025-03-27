$env:PWD = (Get-Location).Path
docker compose down
# docker image prune -a -f
docker compose pull
docker compose up -d
Write-Output "NOMAD up at $(Get-Date -Format u)"