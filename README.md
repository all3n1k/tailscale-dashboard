# Tailnet Dashboard

A self-hosted web dashboard for monitoring and controlling a personal Tailscale mesh network. Runs as a single-file Flask server — no build step, no database, no container required.

Live at `http://localhost:5555` on `neo-mac` (the always-on MacBook Air hub).

---

## Features

- **Real-time device grid** — online/offline status, latency, OS icons, rx/tx bytes, connection type (direct vs relay)
- **Soft refresh** — polls every 15 seconds; only re-renders cards whose data changed (no flash, no focus loss)
- **WebSocket SSH terminal** — full xterm.js terminal in the browser; PTY-based on Unix, paramiko channel on Windows
- **Wake-on-LAN** — magic packet to wake offline machines (one click, no config)
- **Remote power controls** — shutdown and restart any machine over SSH with confirmation dialog
- **Command library** — searchable, categorized SSH command palette with per-machine output panel
- **iPhone Mirroring control** — screenshot + tap/swipe/type via Mirroir MCP (macOS only)
- **Cross-platform launchers** — `start.command` (macOS) and `start.bat` (Windows) handle deps + start + browser open in one click
- **MCP server** — `mcp_server.py` exposes the tailnet as callable tools directly to Claude

---

## Network Topology

| Hostname | Label | OS | SSH User | SSH Port | Category |
|---|---|---|---|---|---|
| neo-mac | MacBook Air | macOS | neo | 22 | laptop |
| digitalstorm | Digital Storm Workstation | Windows | ALLEN | 22 | desktop |
| neo | Neo Laptop | Windows | allen | 22 | laptop |
| michellepc | Michelle's PC | Windows | michelle | 22 | desktop |
| a-pad | A-Pad | Windows | allen | 22 | laptop |
| galaxy-tab-a7-lite | Galaxy Tab | Android | neo | 8022 | tablet |
| serverbox | Server Box | Windows | dev303 | 22 | desktop |
| optiserver | Opti Server | Windows | subst | 22 | desktop |

## Hardware Specs

| Machine | CPU | GPU | RAM |
|---|---|---|---|
| neo-mac | Apple M-series | — | — |
| digitalstorm | Intel i9-12900K | RTX 3090 24GB | — (offline during scan) |
| neo | Intel i7-12800H | RTX 3080 Ti 16GB | 32GB |
| michellepc | Intel i7-9700K | GTX 1660 Super | 32GB |
| serverbox | Intel i5-12500T | Intel UHD 770 | 16GB |
| optiserver | Intel i5-12500T | Intel UHD 770 | 16GB |
| a-pad | Intel Celeron N4120 | Intel UHD 600 | 8GB |

---

## Setup

### Dependencies

```bash
pip install flask flask-sock paramiko
```

Or use the requirements file:

```bash
pip install -r requirements.txt
```

### Running

**macOS** — double-click `start.command` in Finder, or:
```bash
./start.command
```

**Windows** — double-click `start.bat`, or run in cmd:
```
start.bat
```

**Manual:**
```bash
python3 dashboard.py          # default port 5555
PORT=8080 python3 dashboard.py  # custom port
```

**Background (survives terminal close):**
```bash
pkill -f "dashboard.py" 2>/dev/null; sleep 1
nohup python3 /Users/neo/tailscale-dashboard/dashboard.py \
  > /Users/neo/tailscale-dashboard/dashboard.log 2>&1 &
```

---

## SSH Key Setup

The dashboard SSHs into peers using `~/.ssh/id_ed25519` on the host machine. The public key must be authorized on each peer.

### Windows (administrator accounts)
The key goes in a special path — NOT `~\.ssh\authorized_keys`:
```
C:\ProgramData\ssh\administrators_authorized_keys
```

Permissions must be restricted to SYSTEM and Administrators only. Use `icacls` via `cmd.exe` with SID syntax (PowerShell's `/` flag parsing breaks):
```cmd
cmd /c "icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant *S-1-5-18:F /grant *S-1-5-32-544:F"
```

If the file is locked after first sshd start, reclaim it with `takeown` first:
```cmd
cmd /c "takeown /f C:\ProgramData\ssh\administrators_authorized_keys"
cmd /c "icacls C:\ProgramData\ssh\administrators_authorized_keys /grant Administrators:F"
```

### Linux / macOS
```bash
~/.ssh/authorized_keys   # standard path, chmod 600
```

### Android (Galaxy Tab via Termux)
```bash
~/.ssh/authorized_keys   # Termux home
# sshd runs on port 8022 — start with: sshd
```

---

## File Structure

```
tailscale-dashboard/
├── dashboard.py       # Everything: Flask app, API routes, WebSocket SSH, HTML/CSS/JS (monolith)
├── mcp_server.py      # MCP stdio server — exposes tailnet tools to Claude
├── requirements.txt   # flask, flask-sock, paramiko
├── start.command      # macOS double-click launcher
├── start.bat          # Windows double-click launcher
├── CLAUDE.md          # Project context for Claude Code sessions
└── dashboard.log      # Server stdout (nohup output, gitignored)
```

---

## Architecture

### Python Layer (`dashboard.py`)

**Tailscale status cache** (`_tailscale_status`)
Runs `tailscale status --json` and caches the result for 3 seconds. Prevents redundant subprocess spawns when multiple API calls arrive simultaneously (e.g. during parallel ping + status fetch).

**SSH WebSocket endpoint** (`/ssh`)
- Accepts a WebSocket connection; first message is `{ip, username, port, cols, rows}`
- **Unix**: opens a PTY with `pty.openpty()`, calls `tty.setraw()` to disable line buffering, spawns `ssh -tt` with stdin/stdout/stderr attached to the PTY master, bridges PTY ↔ WebSocket via `select()`
- **Windows**: connects via `paramiko.SSHClient.invoke_shell()`, non-blocking channel poll loop
- Message protocol: `{t:"o", d:"..."}` (output), `{t:"i", d:"..."}` (input), `{t:"r", cols, rows}` (resize), `{t:"closed"}` (session end)

**Ping** (`ping_device`)
Cross-platform: `ping -c 1 -W 2000` on Unix, `ping -n 1 -w 2000` on Windows. Runs in a `ThreadPoolExecutor` with 12 workers across all online peers simultaneously.

**Wake-on-LAN** (`_send_wol`)
Sends a 102-byte UDP magic packet (6×`0xFF` + 16× MAC bytes) to `255.255.255.255:9` via a broadcast socket.

**Power control** (`/api/power`)
Fire-and-forget SSH (`subprocess.Popen`, not `run`) — the connection dying on shutdown is expected. OS-aware commands: `shutdown /s /t 0` / `shutdown /r /t 0` on Windows; `sudo shutdown -h now` / `sudo reboot` on Unix.

**MirroirClient**
Persistent `npx -y mirroir-mcp` subprocess with a threading lock. Sends JSON-RPC 2.0 `tools/call` requests, handles stale window detection. Module-level singleton.

### Frontend (inline HTML string)

**Tech stack**: Vanilla JS, no frameworks. xterm.js v5.3.0 + xterm-addon-fit loaded lazily from CDN on first Terminal click.

**Design**: Dark navy (`#0A0E17`) base, Inter font, ambient gradient orbs, pill-shaped status indicators with glow dots, glassmorphism modals.

**Soft refresh**:
```javascript
function cardHash(d) {
  return [d.online, d.latency, d.direct, d.relay,
          d.rx_bytes, d.tx_bytes, d.last_seen, d.last_handshake].join('|');
}
// On refresh: compare data-hash on each card; only replaceWith() on changes
```

**Event delegation**: Terminal, mirror, power, WoL, and command-run buttons are all handled on `#grid` via `.closest()` — buttons survive card re-renders without needing re-binding.

---

## API Reference

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/api/status` | — | All devices with latency, bytes, connection info |
| GET | `/api/mirror/screenshot` | — | Base64 PNG of iPhone screen |
| POST | `/api/mirror/action` | `{action, ...args}` | Tap, swipe, type, home, launch |
| POST | `/api/wol` | `{hostname}` | Send Wake-on-LAN magic packet |
| POST | `/api/power` | `{hostname, action}` | `action`: `"shutdown"` or `"restart"` |
| POST | `/api/run` | `{hostname, command}` | Run shell command over SSH, return output |
| WS | `/ssh` | first msg: `{ip, username, port, cols, rows}` | Interactive SSH terminal |

### `/api/status` response shape (per device)
```json
{
  "hostname": "neo",
  "label": "Neo Laptop",
  "specs": "i7-12800H · RTX 3080 Ti · 32GB · Windows",
  "category": "laptop",
  "ip": "100.121.253.75",
  "os": "windows",
  "online": true,
  "is_self": false,
  "direct": true,
  "relay": "",
  "latency": 4.2,
  "rx_bytes": "1.23GB",
  "tx_bytes": "456.7MB",
  "last_seen": "just now",
  "last_handshake": "2m ago",
  "ssh_user": "allen",
  "ssh_port": 22,
  "has_ssh": true,
  "has_power": true,
  "has_mirror": false,
  "mac": ""
}
```

---

## MCP Server (`mcp_server.py`)

Exposes the tailnet as callable tools for Claude.

**Register:**
```bash
claude mcp add --transport stdio tailnet-ssh -- python3 /Users/neo/tailscale-dashboard/mcp_server.py
```

**Tools:**

| Tool | Description |
|---|---|
| `tailnet_list_devices` | List all devices with status, IP, OS, relay |
| `tailnet_run` | SSH into a peer and run a command (30s timeout) |
| `tailnet_run_local` | Run a command locally on neo-mac |

> Changes to `mcp_server.py` only take effect after restarting the Claude CLI or killing the process.

---

## Known Gaps

- **digitalstorm specs**: Was offline during hardware scan — specs are estimates. Run CIM queries when online.
- **digitalstorm + neo MACs**: Were offline during ARP scan — WoL not available until MACs are added to `DEVICE_MACS` in `dashboard.py`.
- **mcp_server.py SSH ports**: `tailnet_run` always uses port 22 — Galaxy Tab (port 8022) unreachable without adding `SSH_PORTS` dict.
- **dashboard.py modularization**: Still a monolith. Natural split: `config.py`, `routes/`, `templates/`, `mirroir_client.py`.

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| PTY-based SSH (Unix) | Zero extra deps, full terminal emulation (resize, colors, interactive programs like `vim`) |
| paramiko (Windows) | No PTY available on Windows; paramiko is pure Python and handles the same WebSocket protocol |
| Tailscale status cache (3s TTL) | Prevents redundant `tailscale status --json` CLI spawns on concurrent requests |
| Soft refresh via `data-hash` | Prevents input focus loss and visual flash on every 15s poll |
| nohup background process | Cursor's F5 debug launch was unreliable for persistence across edits |
| `cmd /c icacls` for Windows ACLs | PowerShell's `/` flag parsing breaks `icacls` — must run through `cmd.exe` |
| Fire-and-forget SSH for power | SSH connection dies when machine shuts down; `Popen` avoids a hung request |
