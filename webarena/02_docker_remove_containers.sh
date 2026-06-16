#!/bin/bash

# Remove both the original setup containers and the hot-swap pool containers
# created by reset_server/server.py, e.g. shopping_0 or gitlab_4.
BASE_CONTAINERS=(shopping shopping_admin forum gitlab wikipedia openstreetmap-website-web-1 openstreetmap-website-db-1)
declare -A CONTAINERS=()

for container in "${BASE_CONTAINERS[@]}"; do
  CONTAINERS["$container"]=1
done

while IFS= read -r container; do
  if [[ "$container" =~ ^(shopping|shopping_admin|forum|gitlab|wikipedia)(_[0-9]+)?$ ]] || \
     [[ "$container" =~ ^openstreetmap-website-(web|db)-1$ ]]; then
    CONTAINERS["$container"]=1
  fi
done < <(podman ps -a --format '{{.Names}}' 2>/dev/null || true)

for container in "${!CONTAINERS[@]}"; do
  podman stop "$container" 2>/dev/null || true
  podman rm "$container" 2>/dev/null || true
done

podman network rm osm-net 2>/dev/null || true
