# Tailnet Dashboard architecture

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
  "hostname": "my-laptop",
  "label": "My Laptop",
  "specs": "i7 · RTX 3080 · 32GB · Windows",
  "category": "laptop",
  "ip": "100.x.x.x",
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
  "ssh_user": "alice",
  "ssh_port": 22,
  "has_ssh": true,
  "has_power": true,
  "has_mirror": false,
  "mac": ""
}
```

---
