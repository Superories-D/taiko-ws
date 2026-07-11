# Taiko Web multiplayer server

This is the standalone service used by the Taiko Web main site. It serves the game WebSocket protocol at `/` and a public, read-only health check at `/health` on the same port.

## Deploy with Docker Compose

Copy this directory to the target Linux server, ensure Docker Engine and Docker Compose are installed, then run:

```bash
chmod +x setup.sh
sudo ./setup.sh
```

The first run asks for the public origin of the Taiko Web site, the port, and a hard online-user limit. The script calculates a conservative recommendation from CPU and RAM (50 users per vCPU, additionally limited by roughly 10 MiB per user). Start at or below that recommendation and raise it only after monitoring the host. For unattended deployment, pass them as environment variables:

```bash
sudo env ALLOWED_ORIGINS=https://taiko.example.com PORT=34802 MAX_CONNECTIONS=100 ./setup.sh
```

Open the selected TCP port in the host firewall. When the game site uses HTTPS, place this service behind TLS or otherwise publish it as `wss://…`; browsers will block insecure `ws://` connections from an HTTPS site.

## Add it to Taiko Web

In **Admin → Multiplayer**, add the public WebSocket origin, for example `wss://multiplayer.example.com`, and set the same online limit as `MAX_CONNECTIONS`. The main site derives and checks `https://multiplayer.example.com/health`, excludes full nodes, and balances new connections by capacity utilisation first and health-check latency second. WebSocket game traffic never passes through the main site.

`ALLOWED_ORIGINS` is a comma-separated exact origin allowlist. Do not use a wildcard. The health endpoint intentionally exposes only service state, aggregate connection count, and capacity. Any compatible third-party node must return `status: "ok"`, an integer `connections`, and a boolean `accepting_connections`; otherwise the main site will not route players to it. The service enforces `MAX_CONNECTIONS` at both handshake and connection time, limits message size and queueing, and isolates malformed client messages so one client cannot crash the process.
