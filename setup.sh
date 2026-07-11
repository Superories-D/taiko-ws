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

"${COMPOSE[@]}" up -d --build
"${COMPOSE[@]}" ps
echo "Deployment complete. Verify with: curl http://127.0.0.1:$(grep '^PORT=' .env | cut -d= -f2)/health"
echo "Set the same online limit in Taiko Web Admin → Multiplayer. This server will still reject joins at MAX_CONNECTIONS as a safety backstop."
