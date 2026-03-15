#!/usr/bin/env python3
"""
Shoutcast → Icecast 2 Relay Server
Connects to a Shoutcast source, reads ICY audio + metadata,
and pushes it to an Icecast 2 server via the SOURCE protocol.
Includes a Flask web UI for management.
"""

import threading
import time
import struct
import socket
import logging
import json
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from flask import Flask, jsonify, request as flask_request, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILE = os.environ.get("RELAY_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))

DEFAULT_CONFIG = {
    "shoutcast_url": "http://your-shoutcast-server:8000",
    "icecast_host": "localhost",
    "icecast_port": 8000,
    "icecast_mount": "/relay",
    "icecast_user": "source",
    "icecast_password": "hackme",
    "icecast_admin_user": "admin",
    "icecast_admin_password": "hackme",
    "icecast_name": "Shoutcast Relay",
    "icecast_description": "Relayed from Shoutcast",
    "icecast_genre": "Various",
    "icecast_url": "http://localhost",
    "user_agent": "ShoutcastRelay/1.0",
    "reconnect_delay": 5,
    "web_host": "0.0.0.0",
    "web_port": 8080,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    # Environment variables override everything (RELAY_SHOUTCAST_URL, etc.)
    for key in DEFAULT_CONFIG:
        env_key = "RELAY_" + key.upper()
        if env_key in os.environ:
            val = os.environ[env_key]
            # cast to int if default is int
            if isinstance(DEFAULT_CONFIG[key], int):
                val = int(val)
            cfg[key] = val
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Relay state
# ---------------------------------------------------------------------------

@dataclass
class RelayState:
    running: bool = False
    connected_shoutcast: bool = False
    connected_icecast: bool = False
    current_metadata: str = ""
    content_type: str = ""
    bitrate: str = ""
    station_name: str = ""
    bytes_relayed: int = 0
    started_at: Optional[str] = None
    last_error: str = ""
    metadata_history: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["uptime"] = self._uptime()
        d["bytes_relayed_human"] = self._human_bytes()
        return d

    def _uptime(self) -> str:
        if not self.started_at:
            return "—"
        try:
            start = datetime.fromisoformat(self.started_at)
            delta = datetime.now() - start
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            return "—"

    def _human_bytes(self) -> str:
        b = self.bytes_relayed
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"


state = RelayState()
relay_thread: Optional[threading.Thread] = None
stop_event = threading.Event()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Icecast SOURCE connection  (HTTP-based source protocol)
# ---------------------------------------------------------------------------

def connect_icecast(cfg: dict) -> socket.socket:
    """Open a raw TCP socket and send an Icecast SOURCE request."""
    import base64

    host = cfg["icecast_host"]
    port = int(cfg["icecast_port"])
    mount = cfg["icecast_mount"]
    if not mount.startswith("/"):
        mount = "/" + mount

    credentials = base64.b64encode(
        f'{cfg["icecast_user"]}:{cfg["icecast_password"]}'.encode()
    ).decode()

    sock = socket.create_connection((host, port), timeout=10)

    # Use SOURCE method (legacy) which is widely supported
    header = (
        f"SOURCE {mount} HTTP/1.0\r\n"
        f"Authorization: Basic {credentials}\r\n"
        f"User-Agent: {cfg['user_agent']}\r\n"
        f"Content-Type: {state.content_type or 'audio/mpeg'}\r\n"
        f"ice-name: {cfg['icecast_name']}\r\n"
        f"ice-description: {cfg['icecast_description']}\r\n"
        f"ice-genre: {cfg['icecast_genre']}\r\n"
        f"ice-url: {cfg['icecast_url']}\r\n"
        f"ice-public: 1\r\n"
        f"\r\n"
    )
    sock.sendall(header.encode())

    # Read response
    resp = b""
    while b"\r\n\r\n" not in resp and b"\n\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Icecast closed connection before response")
        resp += chunk

    resp_str = resp.decode("utf-8", errors="replace")
    first_line = resp_str.split("\r\n")[0] if "\r\n" in resp_str else resp_str.split("\n")[0]
    if "200" not in first_line:
        raise ConnectionError(f"Icecast rejected connection: {first_line}")

    log.info("Connected to Icecast at %s:%d%s", host, port, mount)
    return sock


# ---------------------------------------------------------------------------
# Icecast metadata update via admin API
# ---------------------------------------------------------------------------

def update_icecast_metadata(cfg: dict, metadata: str):
    """Push metadata to Icecast via the admin API."""
    try:
        mount = cfg["icecast_mount"]
        if not mount.startswith("/"):
            mount = "/" + mount
        url = (
            f'http://{cfg["icecast_host"]}:{cfg["icecast_port"]}'
            f"/admin/metadata?mode=updinfo&mount={mount}"
            f"&song={requests.utils.quote(metadata)}"
        )
        requests.get(
            url,
            auth=(cfg.get("icecast_admin_user", "admin"), cfg.get("icecast_admin_password", cfg["icecast_password"])),
            timeout=5,
        )
        log.info("Metadata updated: %s", metadata)
    except Exception as e:
        log.warning("Failed to update metadata: %s", e)


# ---------------------------------------------------------------------------
# Shoutcast ICY stream reader
# ---------------------------------------------------------------------------

def relay_loop(cfg: dict):
    """Main relay loop: connect to Shoutcast, pipe audio to Icecast."""
    global state

    while not stop_event.is_set():
        ice_sock: Optional[socket.socket] = None
        try:
            # ---- Connect to Shoutcast ----
            log.info("Connecting to Shoutcast: %s", cfg["shoutcast_url"])
            headers = {
                "User-Agent": cfg["user_agent"],
                "Icy-MetaData": "1",  # request inline metadata
            }
            resp = requests.get(
                cfg["shoutcast_url"],
                headers=headers,
                stream=True,
                timeout=(10, None),  # (connect_timeout, read_timeout=infinite)
            )
            resp.raise_for_status()

            icy_metaint = int(resp.headers.get("icy-metaint", 0))
            state.content_type = resp.headers.get("content-type", "audio/mpeg")
            state.bitrate = resp.headers.get("icy-br", "?")
            state.station_name = resp.headers.get("icy-name", "Unknown")
            state.connected_shoutcast = True
            log.info(
                "Shoutcast connected — type=%s bitrate=%s metaint=%d",
                state.content_type, state.bitrate, icy_metaint,
            )

            # ---- Connect to Icecast ----
            ice_sock = connect_icecast(cfg)
            state.connected_icecast = True
            state.started_at = datetime.now().isoformat()
            state.last_error = ""

            # ---- Read & relay ----
            raw_iter = resp.iter_content(chunk_size=8192)

            if icy_metaint > 0:
                _relay_with_metadata(raw_iter, ice_sock, icy_metaint, cfg)
            else:
                _relay_plain(raw_iter, ice_sock)

        except Exception as e:
            log.error("Relay error: %s", e)
            state.last_error = str(e)
        finally:
            state.connected_shoutcast = False
            state.connected_icecast = False
            if ice_sock:
                try:
                    ice_sock.close()
                except Exception:
                    pass

        if not stop_event.is_set():
            delay = int(cfg.get("reconnect_delay", 5))
            log.info("Reconnecting in %d seconds…", delay)
            stop_event.wait(delay)

    log.info("Relay loop stopped.")


def _relay_with_metadata(raw_iter, ice_sock: socket.socket, metaint: int, cfg: dict):
    """Read ICY stream with inline metadata, strip metadata, relay audio."""
    buf = b""
    audio_remaining = metaint  # bytes of audio until next metadata block

    for chunk in raw_iter:
        if stop_event.is_set():
            return
        buf += chunk

        while len(buf) > 0:
            if audio_remaining > 0:
                # Consume audio bytes
                take = min(audio_remaining, len(buf))
                audio_data = buf[:take]
                buf = buf[take:]
                audio_remaining -= take
                ice_sock.sendall(audio_data)
                state.bytes_relayed += len(audio_data)
                if state.bytes_relayed % (256 * 1024) < len(audio_data):
                    log.info("Relayed %s so far", state._human_bytes())
            else:
                # Next byte is the metadata length indicator
                if len(buf) < 1:
                    break
                meta_len = buf[0] * 16
                buf = buf[1:]
                if meta_len > 0:
                    if len(buf) < meta_len:
                        # Need more data; put the length byte back conceptually
                        # and break — we'll resume next chunk.
                        buf = bytes([meta_len // 16]) + buf
                        audio_remaining = 0  # still in metadata state
                        break
                    meta_bytes = buf[:meta_len]
                    buf = buf[meta_len:]
                    _parse_icy_metadata(meta_bytes, cfg)
                audio_remaining = metaint


def _relay_plain(raw_iter, ice_sock: socket.socket):
    """Relay without metadata parsing."""
    for chunk in raw_iter:
        if stop_event.is_set():
            return
        ice_sock.sendall(chunk)
        state.bytes_relayed += len(chunk)


def _parse_icy_metadata(data: bytes, cfg: dict):
    """Extract StreamTitle from ICY metadata block."""
    try:
        text = data.decode("utf-8", errors="replace").rstrip("\x00")
        if "StreamTitle='" in text:
            title = text.split("StreamTitle='", 1)[1].split("';", 1)[0]
            if title and title != state.current_metadata:
                state.current_metadata = title
                state.metadata_history.insert(0, {
                    "title": title,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
                state.metadata_history = state.metadata_history[:50]
                update_icecast_metadata(cfg, title)
    except Exception as e:
        log.warning("Metadata parse error: %s", e)


# ---------------------------------------------------------------------------
# Flask Web UI
# ---------------------------------------------------------------------------

app = Flask(__name__)
current_config = load_config()


@app.route("/")
def index():
    return HTML_PAGE


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shoutcast → Icecast Relay</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0c0e11;
    --surface: #14171c;
    --surface2: #1b1f27;
    --border: #262b35;
    --text: #d4d8e0;
    --text-dim: #6b7280;
    --accent: #22d3ee;
    --accent-dim: rgba(34,211,238,0.12);
    --red: #f87171;
    --green: #4ade80;
    --orange: #fb923c;
    --font-mono: 'JetBrains Mono', monospace;
    --font-sans: 'DM Sans', sans-serif;
  }

  * { margin:0; padding:0; box-sizing:border-box; }

  body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  .container { max-width: 860px; margin: 0 auto; padding: 2rem 1.5rem; }

  /* Header */
  header {
    display: flex; align-items: center; gap: 1rem;
    margin-bottom: 2.5rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1.5rem;
  }
  .logo {
    width: 44px; height: 44px;
    background: var(--accent-dim);
    border: 1px solid rgba(34,211,238,0.25);
    border-radius: 10px;
    display: grid; place-items: center;
    font-size: 1.3rem;
  }
  header h1 {
    font-family: var(--font-mono);
    font-size: 1.1rem; font-weight: 700;
    letter-spacing: -0.02em;
  }
  header h1 span { color: var(--accent); }
  header p { font-size: .78rem; color: var(--text-dim); margin-top: 2px; }

  /* Status Bar */
  .status-bar {
    display: flex; gap: .75rem; flex-wrap: wrap;
    margin-bottom: 1.5rem;
  }
  .pill {
    font-family: var(--font-mono);
    font-size: .72rem;
    padding: .35rem .7rem;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    display: flex; align-items: center; gap: .4rem;
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--text-dim);
    flex-shrink: 0;
  }
  .dot.on { background: var(--green); box-shadow: 0 0 6px rgba(74,222,128,.5); }
  .dot.error { background: var(--red); box-shadow: 0 0 6px rgba(248,113,113,.4); }

  /* Cards */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 1rem;
  }
  .card-title {
    font-family: var(--font-mono);
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-dim);
    margin-bottom: .85rem;
  }

  /* Now Playing */
  .now-playing {
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--accent);
    word-break: break-word;
    min-height: 1.6em;
  }
  .now-playing.idle { color: var(--text-dim); font-weight: 400; font-style: italic; }

  /* Stats grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: .75rem;
  }
  .stat { text-align: center; padding: .5rem 0; }
  .stat-val {
    font-family: var(--font-mono);
    font-size: 1.2rem; font-weight: 700;
  }
  .stat-label { font-size: .7rem; color: var(--text-dim); margin-top: .15rem; }

  /* Controls */
  .controls { display: flex; gap: .75rem; margin-bottom: 1.5rem; }
  button {
    font-family: var(--font-mono);
    font-size: .78rem; font-weight: 600;
    padding: .6rem 1.4rem;
    border-radius: 7px;
    border: 1px solid var(--border);
    cursor: pointer;
    transition: all .15s;
    background: var(--surface2);
    color: var(--text);
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  .btn-start { background: rgba(74,222,128,.1); border-color: rgba(74,222,128,.3); color: var(--green); }
  .btn-start:hover { background: rgba(74,222,128,.2); }
  .btn-stop { background: rgba(248,113,113,.1); border-color: rgba(248,113,113,.3); color: var(--red); }
  .btn-stop:hover { background: rgba(248,113,113,.2); }

  /* Config form */
  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: .6rem .85rem;
  }
  .form-grid .full { grid-column: 1 / -1; }
  label {
    display: block;
    font-family: var(--font-mono);
    font-size: .68rem;
    color: var(--text-dim);
    margin-bottom: .25rem;
    text-transform: uppercase;
    letter-spacing: .05em;
  }
  input[type="text"], input[type="number"], input[type="password"] {
    width: 100%;
    font-family: var(--font-mono);
    font-size: .82rem;
    padding: .5rem .65rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    outline: none;
    transition: border .15s;
  }
  input:focus { border-color: var(--accent); }

  /* History */
  .history-list { max-height: 200px; overflow-y: auto; }
  .history-item {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: .35rem 0;
    border-bottom: 1px solid var(--border);
    font-size: .82rem;
  }
  .history-item:last-child { border-bottom: none; }
  .history-time {
    font-family: var(--font-mono);
    font-size: .7rem;
    color: var(--text-dim);
    flex-shrink: 0;
    margin-left: .75rem;
  }

  /* Error banner */
  .error-banner {
    background: rgba(248,113,113,.08);
    border: 1px solid rgba(248,113,113,.25);
    border-radius: 8px;
    padding: .6rem 1rem;
    font-size: .8rem;
    color: var(--red);
    margin-bottom: 1rem;
    font-family: var(--font-mono);
    display: none;
  }
  .error-banner.show { display: block; }

  /* Tabs */
  .tabs { display: flex; gap: 0; margin-bottom: 1rem; }
  .tab {
    font-family: var(--font-mono);
    font-size: .75rem;
    padding: .5rem 1rem;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    color: var(--text-dim);
    transition: all .15s;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  @media (max-width: 540px) {
    .form-grid { grid-template-columns: 1fr; }
    .stats-grid { grid-template-columns: 1fr 1fr; }
  }
</style>
</head>
<body>
<div class="container">

  <header>
    <div class="logo">📡</div>
    <div>
      <h1>Shoutcast <span>→</span> Icecast</h1>
      <p>Stream Relay Server</p>
    </div>
  </header>

  <!-- Status pills -->
  <div class="status-bar">
    <div class="pill"><div class="dot" id="dot-relay"></div> <span id="lbl-relay">Stopped</span></div>
    <div class="pill"><div class="dot" id="dot-sc"></div> <span id="lbl-sc">Shoutcast</span></div>
    <div class="pill"><div class="dot" id="dot-ic"></div> <span id="lbl-ic">Icecast</span></div>
  </div>

  <!-- Error -->
  <div class="error-banner" id="error-banner"></div>

  <!-- Controls -->
  <div class="controls">
    <button class="btn-start" id="btn-start" onclick="startRelay()">▶ Start</button>
    <button class="btn-stop" id="btn-stop" onclick="stopRelay()">■ Stop</button>
  </div>

  <!-- Now Playing -->
  <div class="card">
    <div class="card-title">Now Playing</div>
    <div class="now-playing idle" id="now-playing">Waiting for stream…</div>
  </div>

  <!-- Stats -->
  <div class="card">
    <div class="card-title">Stats</div>
    <div class="stats-grid">
      <div class="stat"><div class="stat-val" id="s-uptime">—</div><div class="stat-label">Uptime</div></div>
      <div class="stat"><div class="stat-val" id="s-bytes">0 B</div><div class="stat-label">Relayed</div></div>
      <div class="stat"><div class="stat-val" id="s-bitrate">—</div><div class="stat-label">Bitrate</div></div>
      <div class="stat"><div class="stat-val" id="s-type">—</div><div class="stat-label">Format</div></div>
    </div>
  </div>

  <!-- Tabs: History / Config -->
  <div class="tabs">
    <div class="tab active" onclick="switchTab('history')">History</div>
    <div class="tab" onclick="switchTab('config')">Configuration</div>
  </div>

  <div id="panel-history" class="tab-panel active">
    <div class="card">
      <div class="history-list" id="history-list">
        <div style="color:var(--text-dim); font-size:.82rem; font-style:italic;">No metadata yet.</div>
      </div>
    </div>
  </div>

  <div id="panel-config" class="tab-panel">
    <div class="card">
      <div class="card-title">Shoutcast Source</div>
      <div class="form-grid">
        <div class="full">
          <label>Stream URL</label>
          <input type="text" id="cfg-shoutcast_url">
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Icecast Destination</div>
      <div class="form-grid">
        <div><label>Host</label><input type="text" id="cfg-icecast_host"></div>
        <div><label>Port</label><input type="number" id="cfg-icecast_port"></div>
        <div><label>Mount Point</label><input type="text" id="cfg-icecast_mount"></div>
        <div><label>Source User</label><input type="text" id="cfg-icecast_user"></div>
        <div><label>Password</label><input type="password" id="cfg-icecast_password"></div>
        <div><label>Reconnect Delay (s)</label><input type="number" id="cfg-reconnect_delay"></div>
        <div><label>Admin User</label><input type="text" id="cfg-icecast_admin_user"></div>
        <div><label>Admin Password</label><input type="password" id="cfg-icecast_admin_password"></div>
        <div class="full"><label>Station Name</label><input type="text" id="cfg-icecast_name"></div>
        <div class="full"><label>Description</label><input type="text" id="cfg-icecast_description"></div>
        <div><label>Genre</label><input type="text" id="cfg-icecast_genre"></div>
        <div><label>Station URL</label><input type="text" id="cfg-icecast_url"></div>
      </div>
      <br>
      <button onclick="saveConfig()">Save Configuration</button>
    </div>
  </div>

</div>

<script>
const CFG_FIELDS = [
  "shoutcast_url","icecast_host","icecast_port","icecast_mount",
  "icecast_user","icecast_password","icecast_admin_user","icecast_admin_password","icecast_name","icecast_description",
  "icecast_genre","icecast_url","reconnect_delay"
];

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
}

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    // dots
    setDot('dot-relay', s.running);
    setDot('dot-sc', s.connected_shoutcast);
    setDot('dot-ic', s.connected_icecast);
    document.getElementById('lbl-relay').textContent = s.running ? 'Running' : 'Stopped';

    // now playing
    const np = document.getElementById('now-playing');
    if (s.current_metadata) {
      np.textContent = s.current_metadata;
      np.classList.remove('idle');
    } else {
      np.textContent = s.station_name || 'Waiting for stream…';
      np.classList.add('idle');
    }

    // stats
    document.getElementById('s-uptime').textContent = s.uptime;
    document.getElementById('s-bytes').textContent = s.bytes_relayed_human;
    document.getElementById('s-bitrate').textContent = s.bitrate ? s.bitrate + ' kbps' : '—';
    document.getElementById('s-type').textContent = s.content_type || '—';

    // error
    const eb = document.getElementById('error-banner');
    if (s.last_error) { eb.textContent = s.last_error; eb.classList.add('show'); }
    else { eb.classList.remove('show'); }

    // history
    const hl = document.getElementById('history-list');
    if (s.metadata_history && s.metadata_history.length) {
      hl.innerHTML = s.metadata_history.map(h =>
        `<div class="history-item"><span>${esc(h.title)}</span><span class="history-time">${h.time}</span></div>`
      ).join('');
    }

    // buttons
    document.getElementById('btn-start').disabled = s.running;
    document.getElementById('btn-stop').disabled = !s.running;
  } catch(e) { /* ignore polling errors */ }
}

function setDot(id, on) {
  const d = document.getElementById(id);
  d.classList.toggle('on', !!on);
}

function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

async function startRelay() { await fetch('/api/start', {method:'POST'}); fetchStatus(); }
async function stopRelay()  { await fetch('/api/stop',  {method:'POST'}); fetchStatus(); }

async function loadConfig() {
  const r = await fetch('/api/config');
  const c = await r.json();
  CFG_FIELDS.forEach(f => {
    const el = document.getElementById('cfg-' + f);
    if (el) el.value = c[f] ?? '';
  });
}

async function saveConfig() {
  const body = {};
  CFG_FIELDS.forEach(f => {
    const el = document.getElementById('cfg-' + f);
    if (el) body[f] = el.value;
  });
  await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  alert('Configuration saved.');
}

loadConfig();
fetchStatus();
setInterval(fetchStatus, 1500);
</script>
</body>
</html>
"""


@app.route("/api/status")
def api_status():
    return jsonify(state.to_dict())


@app.route("/api/config", methods=["GET"])
def api_config_get():
    safe = {k: v for k, v in current_config.items() if "password" not in k}
    for pk in ("icecast_password", "icecast_admin_password"):
        safe[pk] = "••••••••" if current_config.get(pk) else ""
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    global current_config
    data = flask_request.get_json(force=True)
    for k, v in data.items():
        if k in DEFAULT_CONFIG:
            if "password" in k and v == "••••••••":
                continue
            current_config[k] = v
    save_config(current_config)
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    global relay_thread
    if state.running:
        return jsonify({"ok": False, "error": "Already running"})
    stop_event.clear()
    state.running = True
    state.bytes_relayed = 0
    state.metadata_history.clear()
    relay_thread = threading.Thread(target=relay_loop, args=(current_config,), daemon=True)
    relay_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not state.running:
        return jsonify({"ok": False, "error": "Not running"})
    stop_event.set()
    state.running = False
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\n  🎵  Shoutcast → Icecast Relay")
    print(f"  Web UI: http://localhost:{current_config['web_port']}\n")
    app.run(
        host=current_config.get("web_host", "0.0.0.0"),
        port=int(current_config.get("web_port", 8080)),
        debug=False,
    )
