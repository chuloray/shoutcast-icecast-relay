# Shoutcast → Icecast 2 Relay

A Python relay server that connects to a Shoutcast stream and re-broadcasts it
through an Icecast 2 server, with full ICY metadata passthrough and a web
management dashboard.

## Features

- **Single-stream relay** with auto-reconnect
- **ICY metadata passthrough** — song titles pushed to Icecast in real time
- **Web management UI** on port 8080
- **Docker-ready** — one command to deploy with bundled Icecast, or point to external
- **Environment variable config** — all settings overridable via env vars

## Quick Start (Docker)

```bash
# 1. Copy .env template and edit your settings
cp .env.example .env
nano .env

# 2. Launch everything
docker compose up -d

# 3. Open the web UI → http://localhost:8080
```

This brings up two containers:

| Container          | Port | Purpose                       |
|-------------------|------|-------------------------------|
| `shoutcast-relay` | 8080 | Relay engine + management UI  |
| `icecast`         | 8000 | Icecast 2 server (listeners)  |

**Using an external Icecast?** Remove the `icecast` service from
`docker-compose.yml` and set `RELAY_ICECAST_HOST` to your server's address.

## Quick Start (Standalone)

```bash
pip install -r requirements.txt
python relay.py
```

## Configuration

All settings can be changed three ways (highest priority wins):

1. **Environment variables** — `RELAY_SHOUTCAST_URL`, `RELAY_ICECAST_HOST`, etc.
2. **config.json** — auto-saved when you use the web UI
3. **Defaults** — built into relay.py

| Variable                       | Default        | Description                      |
|-------------------------------|----------------|----------------------------------|
| `RELAY_SHOUTCAST_URL`         | —              | Shoutcast stream URL             |
| `RELAY_ICECAST_HOST`          | `localhost`    | Icecast server hostname          |
| `RELAY_ICECAST_PORT`          | `8000`         | Icecast server port              |
| `RELAY_ICECAST_MOUNT`         | `/relay`       | Mount point on Icecast           |
| `RELAY_ICECAST_USER`          | `source`       | Icecast source username          |
| `RELAY_ICECAST_PASSWORD`      | `hackme`       | Icecast source password          |
| `RELAY_ICECAST_ADMIN_USER`    | `admin`        | Icecast admin user (for metadata)|
| `RELAY_ICECAST_ADMIN_PASSWORD`| `hackme`       | Icecast admin password           |
| `RELAY_RECONNECT_DELAY`       | `5`            | Seconds between reconnects       |
| `RELAY_WEB_PORT`              | `8080`         | Management UI port               |

## Listener URL

Once running, listeners connect to:
```
http://<icecast-host>:<icecast-port><mount>
```
Example: `http://localhost:8000/relay`

## License

MIT
