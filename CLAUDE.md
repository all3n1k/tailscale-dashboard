# Tailnet Dashboard — Project Context for Claude

This file is the single source of truth for understanding this project. Read it before making any changes.

---

## What This Is

A self-hosted web dashboard for monitoring and controlling a personal Tailscale mesh network. It runs as a Flask server on `neo-mac` (the MacBook Air, always-on hub) at `http://localhost:5555`.

Secondary component: an MCP stdio server (`mcp_server.py`) that exposes Tailscale network tools directly to Claude in conversation.

---

## File Structure

```
/Users/neo/tailscale-dashboard/
├── dashboard.py       # Everything: Flask app, API, WebSocket SSH, HTML/CSS/JS (monolith)
├── mcp_server.py      # MCP stdio server — tailnet tools for Claude
├── dashboard.log      # Server stdout/stderr (nohup output)
└── .vscode/
    └── launch.json    # VS Code debugpy config (PORT=5555)
```

No requirements.txt — deps are: `flask`, `flask-sock`. Install with pip if missing.

---

## Running the Server

**Never use Cursor F5 for production.** The background nohup approach is standard:

```bash
# Start (background, survives terminal close)
pkill -f "dashboard.py" 2>/dev/null; sleep 1
nohup python3 /Users/neo/tailscale-dashboard/dashboard.py > /Users/neo/tailscale-dashboard/dashboard.log 2>&1 &

# Verify
curl -s http://localhost:5555/api/status | python3 -m json.tool | head -20

# Check logs
tail -f /Users/neo/tailscale-dashboard/dashboard.log

# Stop
pkill -f "dashboard.py"
```

Port is controlled by `PORT` env var (default 5555).

---

## Network Topology

| Hostname | Label | Tailscale IP | OS | SSH User | SSH Port | Category |
|---|---|---|---|---|---|---|
| neo-mac | MacBook Air | (self) | macOS | neo | 22 | laptop |
| digitalstorm | Digital Storm Workstation | varies | windows | ALLEN | 22 | desktop |
| neo | Neo Laptop | 100.121.253.75 | windows | allen | 22 | laptop |
| michellepc | Michelle's PC | 100.69.0.43 | windows | michelle | 22 | desktop |
| a-pad | A-Pad | 100.125.84.119 | windows | allen | 22 | laptop |
| galaxy-tab-a7-lite | Galaxy Tab | 100.82.118.70 | android | neo | **8022** | tablet |
| allens-iphone | Allen's iPhone | varies | iOS | — | — | mobile |
| iphone-se-gen-2 | iPhone SE | varies | iOS | — | — | mobile |
| iphone172 | iPhone 7 | varies | iOS | — | — | mobile |

**SSH access**: desktop, laptop, tablet categories get Terminal buttons. Mobile does not.

**Galaxy Tab**: Termux sshd on port 8022. Any username accepted (key auth). SSH key is `~/.ssh/id_ed25519` on neo-mac.

**Windows SSH note**: All Windows machines use administrator accounts. The authorized_keys path is `C:\ProgramData\ssh\administrators_authorized_keys` (NOT `~\.ssh\authorized_keys`). Permissions must be restricted to SYSTEM and Administrators only — use `Set-Acl` with PowerShell SIDs, not `icacls /grant` (icacls parameter parsing breaks in PowerShell).

**Digital Storm**: Was offline during the last hardware identification session. Specs in DEVICE_INFO are estimates — needs a hardware scan when it comes online.

---

## Hardware Specs (verified via CIM queries)

| Machine | CPU | GPU | RAM | Notes |
|---|---|---|---|---|
| neo-mac | Apple M-series | — | — | macOS 26 |
| digitalstorm | Intel 12900K | RTX 3090 | — | Estimate — not yet scanned |
| neo | i7-12800H | RTX 3080 Ti Laptop GPU | 32GB | Mobile chip — it's a laptop |
| michellepc | i7-9700K | GTX 1660 Super | 32GB | Was incorrectly listed as 1650 Ti |
| a-pad | Celeron N4120 | Intel UHD 600 | 8GB | Low-power Windows tablet |

---

## dashboard.py Architecture

### Python layer (~400 lines)

**Imports & globals**
- Standard: `json`, `subprocess`, `concurrent.futures`, `re`, `os`, `pty`, `select`, `struct`, `fcntl`, `termios`, `threading`, `time`, `datetime`
- Flask: `Flask`, `jsonify`, `request`; `flask_sock`: `Sock`

**Tailscale status cache** (lines 22–35)
- `_STATUS_TTL = 3` seconds
- `_tailscale_status()` — runs `tailscale status --json`, caches result for 3s to prevent redundant subprocess spawns on concurrent API calls

**MirroirClient** (lines 39–96)
- Persistent `npx -y mirroir-mcp` subprocess wrapper with threading lock
- Auto-restarts if process dies; sends `initialize`/`notifications/initialized` on startup
- `call(tool, args)` → JSON-RPC `tools/call`; handles stale window detection
- Module-level singleton: `mirroir = MirroirClient()`

**Device config** (lines 98–177)
- `DEVICE_INFO` dict: `hostname → {label, specs, role, category}`
- `SSH_USERS` dict: `hostname → unix_username`
- `SSH_PORTS` dict: `hostname → port` (only galaxy-tab-a7-lite: 8022)
- `SSH_DEFAULT_USER = "neo"`
- `OS_ICONS` dict: `os_string → icon_key`

**SSH WebSocket endpoint** `/ssh` (lines 180–244)
- `flask_sock` WebSocket
- First message from client: `{ip, username, port, cols, rows}`
- Opens a PTY, spawns `ssh -tt` into it, bridges PTY ↔ WebSocket
- Message protocol: `{t:"o", d:"..."}` (output), `{t:"i", d:"..."}` (input), `{t:"r", cols, rows}` (resize), `{t:"closed"}` (session end)
- SSH flags: `StrictHostKeyChecking=accept-new`, `ConnectTimeout=8`, `IdentitiesOnly=yes`, `-i ~/.ssh/id_ed25519`, `-p {port}` if non-22

**Utilities** (lines 248–280)
- `ping_device(ip)` — `ping -c 1 -W 2000`; returns avg latency float or None
- `fmt_bytes(b)` — B/KB/MB/GB formatter
- `rel_time(iso)` — ISO timestamp → "just now / Xm ago / Xh ago / Xd ago"

**API routes** (lines 283–394)
- `GET /api/status` — calls `_tailscale_status()`, builds device list, pings all online peers in parallel (ThreadPoolExecutor, 12 workers), returns JSON
- `GET /api/mirror/screenshot` — calls `mirroir.call("screenshot")`, returns base64 PNG
- `POST /api/mirror/action` — body: `{action, ...args}`; action map in `_MIRROR_TOOL_MAP`
- `GET /` — serves the inline HTML string

**Mirror action dispatch** (lines 364–389)
- `_MIRROR_TOOL_MAP` — static dict mapping action names to mirroir tool names (hoisted to module level)
- `_mirror_args(action, d)` — builds per-request args from POST body

**`build()` function** inside `api_status()` (lines 292–320)
Returns per-device dict with these fields:
```
hostname, label, specs, role, category, ip, os, os_icon,
online, is_self, relay, direct, last_seen, last_handshake,
rx_bytes, tx_bytes, latency (filled later), ssh_user, ssh_port,
has_ssh (category in desktop/laptop/tablet),
has_mirror (os in iOS/android)
```

### Frontend (inline HTML string, lines 399–905)

**Tech stack**: Vanilla JS, no frameworks. xterm.js v5.3.0 + xterm-addon-fit v0.8.0 loaded lazily from CDN on first Terminal click.

**Color scheme**: Dark GitHub-style (`--bg: #0d1117`, `--surface: #161b22`)

**Card rendering**
- `cardHash(d)` — joins `[online, latency, direct, relay, rx_bytes, tx_bytes, last_seen, last_handshake]` with `|`
- `cardHTML(d)` — builds full card HTML; card root has `id="card-{hostname}"` and `data-hash="{hash}"`

**Soft refresh** (the key optimization — no flash)
```javascript
async function refresh() {
  // On first load or if device list changes: full innerHTML render
  // Otherwise: iterate devices, compare data-hash, only replaceWith() changed cards
}
setInterval(refresh, 15000);  // polls every 15 seconds
```

**Event delegation**: Terminal/Mirror button clicks are handled on the `#grid` div, not individual buttons (because buttons are re-rendered on update). Uses `.closest('.term-btn')` / `.closest('.mirror-btn')`.

**Terminal modal**: `openTerminal(ip, username, port, label)` — creates xterm.js Terminal, opens WebSocket to `/ssh`, bridges input/output.

**Mirror modal**: `openMirror(label)` — fetches screenshots, click-to-tap via coordinate mapping, auto-refresh every 2500ms toggle.

---

## mcp_server.py Architecture

Stdio MCP server. Register with:
```
claude mcp add --transport stdio tailnet-ssh -- python3 /Users/neo/tailscale-dashboard/mcp_server.py
```

**Important**: This is a long-running process. Code changes only take effect after restarting it (`claude` CLI restart or kill the process).

**Tools exposed**:
- `tailnet_list_devices(online_only?)` — lists all Tailscale devices with status
- `tailnet_run(hostname, command, username?)` — SSH into a Tailscale peer, run command
- `tailnet_run_local(command)` — run command locally on neo-mac

**Tailscale cache**: Same TTL-3s pattern as dashboard.py.

**SSH_USERS in mcp_server.py**: Must be kept in sync with dashboard.py manually. Current values:
```python
SSH_USERS = {
    "neo-mac":      "neo",
    "digitalstorm": "ALLEN",
    "neo":          "allen",
    "michellepc":   "michelle",
    "a-pad":        "allen",
}
SSH_DEFAULT_USER = "neo"
SSH_TIMEOUT = 30
```

Note: mcp_server.py does NOT have SSH_PORTS — it uses port 22 for all hosts. The Galaxy Tab isn't reachable via `tailnet_run` without adding port support.

---

## Known Gaps / Pending Work

1. **Digital Storm specs**: Machine was offline during hardware scan. When online, run:
   ```powershell
   Get-CimInstance Win32_Processor | Select Name,NumberOfCores,NumberOfLogicalProcessors
   Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum | Select @{N='GB';E={$_.Sum/1GB}}
   Get-CimInstance Win32_VideoController | Select Name,AdapterRAM
   ```
   Then update `DEVICE_INFO["digitalstorm"]["specs"]` in dashboard.py.

2. **Neo PC sleep settings**: `powercfg /change standby-timeout-ac 0` hasn't been run on the neo laptop yet (was done on a-pad). Run via `tailnet_run` on "neo".

3. **mcp_server.py SSH port support**: `tailnet_run` always uses port 22 — Galaxy Tab (port 8022) can't be reached. Would need `SSH_PORTS` dict and `-p` flag added to the SSH command.

4. **dashboard.py modularization**: Still a monolith. Natural split would be `config.py`, `routes/`, `templates/`, `mirroir_client.py` — but not done yet.

---

## Key Decisions & Why

- **Soft refresh**: Cards have stable `id="card-{hostname}"` and `data-hash` attributes. Refresh only calls `replaceWith()` on cards whose hash changed. Prevents input focus loss and visual flash on every 15s poll.
- **Tailscale status cache**: 3s TTL prevents redundant `tailscale status --json` CLI calls when multiple requests hit the server quickly.
- **PTY-based SSH terminal**: Uses `pty.openpty()` + `os.read/write` instead of paramiko/asyncssh for zero extra dependencies and full terminal emulation (resize, colors, interactive programs).
- **mirroir-mcp via subprocess**: Avoids importing Python client; npx handles versioning. Persistent process (not per-request) for speed.
- **Windows admin SSH key path**: `C:\ProgramData\ssh\administrators_authorized_keys` is required for administrator accounts. icacls `/grant` syntax breaks in PowerShell — use `Set-Acl` with `FileSystemAccessRule` objects instead.
- **nohup background process**: Cursor's F5 debug launch was unreliable for keeping the server alive across edits. nohup ensures the process persists.
