#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is required. Install Docker Engine and Docker Compose, then rerun setup.sh." >&2
  exit 1
fi

if [ ! -f .env ]; then
  site_origin=${ALLOWED_ORIGINS:-}
  port=${PORT:-34802}
  cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
  memory_kib=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 1048576)
  cpu_recommendation=$((cpu_count * 50))
  memory_recommendation=$((memory_kib / 10240))
  recommendation=$cpu_recommendation
  if [ "$memory_recommendation" -lt "$recommendation" ]; then
    recommendation=$memory_recommendation
  fi
  if [ "$recommendation" -lt 20 ]; then
    recommendation=20
  fi
  if [ "$recommendation" -gt 1000 ]; then
    recommendation=1000
  fi
  max_connections=${MAX_CONNECTIONS:-$recommendation}
  if [ -z "$site_origin" ]; then
    printf 'Allowed Taiko Web origin (for example https://taiko.example.com): '
    read -r site_origin
  fi
  if [ -z "$site_origin" ]; then
    echo "ALLOWED_ORIGINS is required." >&2
    exit 1
  fi
  printf 'WebSocket port [%s]: ' "$port"
  read -r entered_port
  port=${entered_port:-$port}
  if ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "PORT must be between 1 and 65535." >&2
    exit 1
  fi
  printf 'Hard online-user limit [%s] (conservative recommendation for %s vCPU / %s MiB RAM): ' "$max_connections" "$cpu_count" "$((memory_kib / 1024))"
  read -r entered_limit
  max_connections=${entered_limit:-$max_connections}
  if ! [[ "$max_connections" =~ ^[0-9]{1,6}$ ]] || [ "$max_connections" -lt 1 ] || [ "$max_connections" -gt 100000 ]; then
    echo "MAX_CONNECTIONS must be between 1 and 100000." >&2
    exit 1
  fi
  cp .env.example .env
  sed -i "s|^PORT=.*|PORT=${port}|" .env
  sed -i "s|^ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=${site_origin}|" .env
  sed -i "s|^MAX_CONNECTIONS=.*|MAX_CONNECTIONS=${max_connections}|" .env
  echo "Recommended starting limit: ${recommendation}. Monitor CPU, RAM, network latency, and WebSocket close codes before increasing it."
  echo "Created .env. Re-run this script after editing it to apply changes."
fi

deploy_log=$(mktemp)
if ! "${COMPOSE[@]}" up -d --build 2>&1 | tee "$deploy_log"; then
  if grep -q "ContainerConfig" "$deploy_log"; then
    echo "Legacy Docker Compose hit the ContainerConfig recreation bug; removing only this stateless WS project's containers and retrying."
    project_name=${COMPOSE_PROJECT_NAME:-$(basename "$SCRIPT_DIR")}
    container_ids=$(docker ps -aq --filter "label=com.docker.compose.project=${project_name}")
    if [ -n "$container_ids" ]; then
      docker rm -f $container_ids >/dev/null 2>&1 || true
    fi
    docker rm -f taiko-multiplayer-server >/dev/null 2>&1 || true
    "${COMPOSE[@]}" up -d --build
  else
    rm -f "$deploy_log"
    exit 1
  fi
fi
rm -f "$deploy_log"
"${COMPOSE[@]}" ps
health_port=$(grep '^PORT=' .env | cut -d= -f2)
health_url="http://127.0.0.1:${health_port}/health"
health_ready=false
for _attempt in $(seq 1 30); do
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$health_url" >/dev/null 2>&1 && health_ready=true
  else
    docker exec taiko-multiplayer-server python -c \
      "import urllib.request; urllib.request.urlopen('${health_url}', timeout=2).read()" \
      >/dev/null 2>&1 && health_ready=true
  fi
  if [ "$health_ready" = "true" ]; then
    break
  fi
  sleep 1
done
if [ "$health_ready" != "true" ]; then
  echo "Multiplayer container did not become healthy within 30 seconds." >&2
  docker logs --tail 100 taiko-multiplayer-server >&2 2>/dev/null || true
  exit 1
fi
echo "Deployment complete. Health check passed: ${health_url}"
echo "Set the same online limit in Taiko Web Admin → Multiplayer. This server will still reject joins at MAX_CONNECTIONS as a safety backstop."
