#!/usr/bin/env python3
import json
import subprocess
import concurrent.futures
import re
import os
import sys
import select
import socket
import struct
import threading
import time
from datetime import datetime, timezone

_IS_WINDOWS = sys.platform == "win32"
if not _IS_WINDOWS:
    import pty
    import tty
    import fcntl
    import termios
else:
    import paramiko

from flask import Flask, jsonify, request
from flask_sock import Sock

app  = Flask(__name__)
sock = Sock(app)

# ─── Tailscale status cache (avoids duplicate CLI calls within 3 s) ───────────
_STATUS_TTL   = 3  # seconds
_status_cache = {"data": None, "at": 0.0}

def _tailscale_status():
    now = time.monotonic()
    if _status_cache["data"] is not None and now - _status_cache["at"] < _STATUS_TTL:
        return _status_cache["data"]
    r = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    stdout = (r.stdout or "").strip()
    if r.returncode != 0:
        raise RuntimeError(
            f"tailscale status --json failed (exit {r.returncode}): "
            f"{(r.stderr or '').strip() or stdout or 'no output'}"
        )
    if not stdout:
        raise RuntimeError(
            "tailscale status --json returned empty output: "
            + ((r.stderr or "").strip() or "no stderr")
        )
    data = json.loads(stdout)
    _status_cache["data"] = data
    _status_cache["at"]   = now
    return data

# ─── Mirroir client ───────────────────────────────────────────────────────────

class MirroirClient:
    """Persistent mirroir-mcp subprocess wrapper."""
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._id   = 0

    def _ensure(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["npx", "-y", "mirroir-mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._rpc("initialize", params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "tailnet-dashboard", "version": "1.0"},
        })
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _write(self, obj):
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method, params=None):
        self._id += 1
        req_id = self._id
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("mirroir-mcp process closed")
            resp = json.loads(line)
            if resp.get("id") == req_id:
                return resp

    def call(self, tool, args=None):
        with self._lock:
            try:
                self._ensure()
                resp = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
                for item in resp.get("result", {}).get("content", []):
                    if item.get("type") in ("image", "text"):
                        # Stale window — kill process so next call re-detects
                        if item.get("type") == "text" and any(
                            x in item.get("text", "").lower()
                            for x in ("no window", "not running", "failed to capture", "nowindow")
                        ):
                            self._proc = None
                        return item
                return {"type": "text", "text": "ok"}
            except Exception as e:
                self._proc = None
                return {"type": "error", "text": str(e)}

mirroir = MirroirClient()

# ─── Device config ────────────────────────────────────────────────────────────

DEVICE_INFO = {
    "neo-mac": {
        "label": "MacBook Air",
        "specs": "macOS 26 · M-series",
        "role": "This machine",
        "category": "laptop",
    },
    "digitalstorm": {
        "label": "Digital Storm Workstation",
        "specs": "Intel 12900K · RTX 3090 · Windows",
        "role": "Main rig / GPU compute",
        "category": "desktop",
    },
    "neo": {
        "label": "Neo Laptop",
        "specs": "i7-12800H · RTX 3080 Ti · 32GB · Windows",
        "role": "Gaming laptop / GPU compute",
        "category": "laptop",
    },
    "michellepc": {
        "label": "Michelle's PC",
        "specs": "i7-9700K · GTX 1660 Super · 32GB · Windows",
        "role": "Secondary PC",
        "category": "desktop",
    },
    "a-pad": {
        "label": "A-Pad",
        "specs": "Celeron N4120 · Intel UHD 600 · 8GB · Windows",
        "role": "Low-power tablet",
        "category": "laptop",
    },
    "allens-iphone": {
        "label": "Allen's iPhone (old)",
        "specs": "iPhone 16 Pro Max",
        "role": "Stale registration",
        "category": "mobile",
    },
    "iphone-se-gen-2": {
        "label": "iPhone SE",
        "specs": "iPhone SE (Gen 2)",
        "role": "Mobile",
        "category": "mobile",
    },
    "iphone172": {
        "label": "Allen's iPhone",
        "specs": "iPhone 16 Pro Max",
        "role": "Primary mobile",
        "category": "mobile",
    },
    "galaxy-tab-a7-lite": {
        "label": "Galaxy Tab",
        "specs": "Galaxy Tab A7 Lite · Android",
        "role": "Tablet",
        "category": "tablet",
    },
    "serverbox": {
        "label": "Server Box",
        "specs": "i5-12500T · Intel UHD 770 · 16GB · Windows",
        "role": "Server / dev box",
        "category": "desktop",
    },
    "optiserver": {
        "label": "Opti Server",
        "specs": "i5-12500T · Intel UHD 770 · 16GB · Windows",
        "role": "Server",
        "category": "desktop",
    },
}

SSH_USERS = {
    "neo-mac":            "neo",
    "digitalstorm":       "ALLEN",
    "neo":                "allen",
    "michellepc":         "michelle",
    "a-pad":              "allen",
    "galaxy-tab-a7-lite": "neo",   # Termux user (any name works; key auth)
    "serverbox":          "dev303",
    "optiserver":         "subst",
}
SSH_PORTS = {
    "galaxy-tab-a7-lite": 8022,    # Termux sshd
}
SSH_DEFAULT_USER = "neo"

# MAC addresses for Wake-on-LAN (add more as devices are discovered)
DEVICE_MACS = {
    "neo-mac":           "c0:c7:db:a5:16:8d",
    "serverbox":         "6c:3c:8c:03:56:41",
    "optiserver":        "74:e5:f9:68:82:f6",
    "michellepc":        "98:48:27:e3:53:c8",
    "a-pad":             "70:9c:d1:60:84:61",
    "galaxy-tab-a7-lite":"b6:f7:12:c4:06:1c",
    # digitalstorm and neo: MACs unknown (were offline during scan)
}

OS_ICONS = {
    "macOS":   "apple",
    "windows": "windows",
    "iOS":     "mobile",
    "android": "android",
    "linux":   "linux",
}

# ─── SSH WebSocket endpoint ───────────────────────────────────────────────────

@sock.route("/ssh")
def ssh_ws(ws):
    # First message: {ip, username, cols, rows}
    try:
        init = json.loads(ws.receive(timeout=10))
    except Exception:
        return

    ip       = init.get("ip", "")
    username = init.get("username", SSH_DEFAULT_USER)
    port     = int(init.get("port", 22))
    cols     = int(init.get("cols", 80))
    rows     = int(init.get("rows", 24))

    if _IS_WINDOWS:
        _ssh_ws_paramiko(ws, ip, username, port, cols, rows)
    else:
        _ssh_ws_pty(ws, ip, username, port, cols, rows)

    ws.send(json.dumps({"t": "closed"}))


def _ssh_ws_pty(ws, ip, username, port, cols, rows):
    """Unix PTY-based SSH bridge."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    tty.setraw(slave)

    ssh_cmd = ["ssh", "-tt",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=8",
               "-o", "IdentitiesOnly=yes",
               "-i", os.path.expanduser("~/.ssh/id_ed25519")]
    if port != 22:
        ssh_cmd += ["-p", str(port)]
    ssh_cmd.append(f"{username}@{ip}")

    proc = subprocess.Popen(
        ssh_cmd,
        stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=os.setsid, close_fds=True,
    )
    os.close(slave)

    ws_sock = ws.sock

    try:
        while proc.poll() is None:
            r, _, _ = select.select([master, ws_sock], [], [], 0.05)

            if master in r:
                try:
                    data = os.read(master, 4096)
                    ws.send(json.dumps({"t": "o", "d": data.decode("utf-8", errors="replace")}))
                except OSError:
                    break

            if ws_sock in r:
                try:
                    msg = ws.receive(timeout=0.1)
                    if msg:
                        m = json.loads(msg)
                        if m["t"] == "i":
                            os.write(master, m["d"].encode("utf-8"))
                        elif m["t"] == "r":
                            fcntl.ioctl(master, termios.TIOCSWINSZ,
                                        struct.pack("HHHH", int(m["rows"]), int(m["cols"]), 0, 0))
                except Exception:
                    pass
    finally:
        try: proc.terminate()
        except Exception: pass
        try: os.close(master)
        except OSError: pass


def _ssh_ws_paramiko(ws, ip, username, port, cols, rows):
    """Windows paramiko-based SSH bridge (no PTY available)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        key = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser("~/.ssh/id_ed25519"))
        client.connect(ip, port=port, username=username, pkey=key, timeout=8)
    except Exception as e:
        ws.send(json.dumps({"t": "o", "d": f"SSH connection failed: {e}\r\n"}))
        return

    chan = client.invoke_shell(width=cols, height=rows)
    chan.settimeout(0)  # non-blocking recv

    try:
        while True:
            if chan.recv_ready():
                data = chan.recv(4096)
                if not data:
                    break
                ws.send(json.dumps({"t": "o", "d": data.decode("utf-8", errors="replace")}))

            try:
                msg = ws.receive(timeout=0.05)
                if msg:
                    m = json.loads(msg)
                    if m["t"] == "i":
                        chan.send(m["d"])
                    elif m["t"] == "r":
                        chan.resize_pty(width=int(m["cols"]), height=int(m["rows"]))
            except Exception:
                pass

            if chan.closed or chan.exit_status_ready():
                break
    finally:
        try: chan.close()
        except Exception: pass
        try: client.close()
        except Exception: pass


# ─── Wake-on-LAN ──────────────────────────────────────────────────────────────

def _send_wol(mac_str):
    mac = mac_str.replace(":", "").replace("-", "")
    if len(mac) != 12:
        raise ValueError(f"Invalid MAC: {mac_str}")
    payload = b'\xff' * 6 + bytes.fromhex(mac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(payload, ('255.255.255.255', 9))


# ─── Utilities ────────────────────────────────────────────────────────────────

def ping_device(ip):
    try:
        if _IS_WINDOWS:
            r = subprocess.run(["ping", "-n", "1", "-w", "2000", ip],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                m = re.search(r"Average = (\d+)ms", r.stdout)
                if m:
                    return float(m.group(1))
        else:
            r = subprocess.run(["ping", "-c", "1", "-W", "2000", ip],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                m = re.search(r"min/avg/max.*?=\s*[\d.]+/([\d.]+)/", r.stdout)
                if m:
                    return round(float(m.group(1)), 1)
    except Exception:
        pass
    return None


def fmt_bytes(b):
    if b < 1024:       return f"{b}B"
    if b < 1048576:    return f"{b/1024:.1f}KB"
    if b < 1073741824: return f"{b/1048576:.1f}MB"
    return f"{b/1073741824:.2f}GB"


def rel_time(iso):
    if not iso or iso.startswith("0001"):
        return None
    try:
        dt   = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        if mins < 2:    return "just now"
        if mins < 60:   return f"{mins}m ago"
        if mins < 1440: return f"{mins//60}h ago"
        return f"{mins//1440}d ago"
    except Exception:
        return None


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    try:
        data = _tailscale_status()
    except Exception as e:
        # Keep frontend stable: always return the expected shape.
        return jsonify({
            "devices": [],
            "tailnet": "tailnet",
            "total": 0,
            "online": 0,
            "offline": 0,
            "avg_latency": None,
            "updated": datetime.now().isoformat(),
            "error": str(e),
        }), 503
    self_node = data.get("Self", {})
    peers     = data.get("Peer", {})
    tailnet   = data.get("MagicDNSSuffix", "tailnet")

    def build(node, is_self=False):
        dns      = node.get("DNSName", "")
        hostname = dns.split(".")[0].lower() if dns else node.get("HostName", "").lower().replace(" ", "-")
        ip       = (node.get("TailscaleIPs") or [""])[0]
        info     = DEVICE_INFO.get(hostname, {})
        category = info.get("category", "other")
        return {
            "hostname":       hostname,
            "label":          info.get("label", hostname),
            "specs":          info.get("specs", node.get("OS", "")),
            "role":           info.get("role", ""),
            "category":       category,
            "ip":             ip,
            "os":             node.get("OS", "unknown"),
            "os_icon":        OS_ICONS.get(node.get("OS", ""), "device"),
            "online":         True if is_self else node.get("Online", False),
            "is_self":        is_self,
            "relay":          node.get("Relay", ""),
            "direct":         bool(node.get("CurAddr", "")),
            "last_seen":      rel_time(node.get("LastSeen", "")),
            "last_handshake": rel_time(node.get("LastHandshake", "")),
            "rx_bytes":       fmt_bytes(node.get("RxBytes", 0)),
            "tx_bytes":       fmt_bytes(node.get("TxBytes", 0)),
            "latency":        None,
            "ssh_user":       SSH_USERS.get(hostname, SSH_DEFAULT_USER),
            "ssh_port":       SSH_PORTS.get(hostname, 22),
            "has_ssh":        category in ("desktop", "laptop", "tablet"),
            "has_mirror":     node.get("OS", "") in ("iOS", "android"),
            "has_power":      category in ("desktop", "laptop") and not is_self,
            "mac":            DEVICE_MACS.get(hostname, ""),
        }

    devices = [build(self_node, is_self=True)]
    for peer in peers.values():
        devices.append(build(peer))

    to_ping = [(d["hostname"], d["ip"]) for d in devices
               if not d["is_self"] and d["online"] and d["ip"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(ping_device, ip): name for name, ip in to_ping}
        lat  = {futs[f]: f.result() for f in concurrent.futures.as_completed(futs)}

    for d in devices:
        d["latency"] = lat.get(d["hostname"])

    devices.sort(key=lambda d: (not d["is_self"], not d["online"], d["label"].lower()))

    online    = sum(1 for d in devices if d["online"])
    latencies = [d["latency"] for d in devices if d["latency"] is not None]
    avg_lat   = round(sum(latencies) / len(latencies), 1) if latencies else None

    return jsonify({
        "devices":     devices,
        "tailnet":     tailnet,
        "total":       len(devices),
        "online":      online,
        "offline":     len(devices) - online,
        "avg_latency": avg_lat,
        "updated":     datetime.now().isoformat(),
    })


@app.route("/api/mirror/screenshot")
def mirror_screenshot():
    r = mirroir.call("screenshot")
    if r and r.get("type") == "image":
        return jsonify({"data": r["data"], "mime": r.get("mimeType", "image/png")})
    err = r.get("text", "unknown error")
    if "noWindow" in err or "no window" in err.lower():
        err = "iPhone Mirroring window is not visible — bring it to the foreground"
    elif "not running" in err.lower():
        err = "iPhone Mirroring is not running — open the app first"
    return jsonify({"error": err}), 503

# Static action → mirroir tool name (hoisted; args are built per-request since they carry request data)
_MIRROR_TOOL_MAP = {
    "tap":      "tap",
    "home":     "press_home",
    "switcher": "press_app_switcher",
    "type":     "type_text",
    "launch":   "launch_app",
    "swipe_up": "swipe",
}

def _mirror_args(action, d):
    if action == "tap":      return {"x": d.get("x"), "y": d.get("y")}
    if action == "type":     return {"text": d.get("text", "")}
    if action == "launch":   return {"app_name": d.get("app", "")}
    if action == "swipe_up": return {"startX": d.get("x", 195), "startY": 700,
                                     "endX": d.get("x", 195), "endY": 100}
    return {}

@app.route("/api/mirror/action", methods=["POST"])
def mirror_action():
    d      = request.json or {}
    action = d.get("action")
    if action not in _MIRROR_TOOL_MAP:
        return jsonify({"error": "unknown action"}), 400
    r = mirroir.call(_MIRROR_TOOL_MAP[action], _mirror_args(action, d))
    return jsonify({"ok": True, "result": r.get("text", "")})


@app.route("/api/wol", methods=["POST"])
def api_wol():
    hostname = (request.json or {}).get("hostname", "").lower()
    mac = DEVICE_MACS.get(hostname)
    if not mac:
        return jsonify({"error": f"No MAC address known for '{hostname}'"}), 404
    try:
        _send_wol(mac)
        return jsonify({"ok": True, "mac": mac})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/power", methods=["POST"])
def api_power():
    d        = request.json or {}
    hostname = d.get("hostname", "").lower()
    action   = d.get("action", "")

    if action not in ("shutdown", "restart"):
        return jsonify({"error": "action must be 'shutdown' or 'restart'"}), 400

    try:
        ts = _tailscale_status()
    except Exception as e:
        return jsonify({"error": str(e)}), 503
    node_map = {}
    for node in [ts.get("Self", {})] + list(ts.get("Peer", {}).values()):
        dns  = node.get("DNSName", "")
        name = dns.split(".")[0].lower() if dns else node.get("HostName", "").lower().replace(" ", "-")
        node_map[name] = node

    node = node_map.get(hostname)
    if not node:
        return jsonify({"error": f"hostname '{hostname}' not found"}), 404

    ip   = (node.get("TailscaleIPs") or [""])[0]
    os_  = node.get("OS", "").lower()
    user = SSH_USERS.get(hostname, SSH_DEFAULT_USER)
    port = SSH_PORTS.get(hostname, 22)

    if "windows" in os_:
        cmd = "shutdown /s /t 0" if action == "shutdown" else "shutdown /r /t 0"
    else:
        cmd = "sudo shutdown -h now" if action == "shutdown" else "sudo reboot"

    try:
        subprocess.Popen(
            ["ssh",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=8",
             "-o", "IdentitiesOnly=yes",
             "-o", "BatchMode=yes",
             "-i", os.path.expanduser("~/.ssh/id_ed25519"),
             "-p", str(port),
             f"{user}@{ip}", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return HTML


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tailnet Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

  :root {
    --bg:      #0A0E17; --surface: #111827; --surface2: #162032;
    --border:  rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.13);
    --text:    #F1F5F9; --muted:   #8895A7;
    --green:   #22C55E; --blue:    #3A82FF; --cyan:  #22D3EE;
    --yellow:  #F59E0B; --red:     #EF4444; --purple:#A78BFA;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; -webkit-font-smoothing: antialiased; }

  /* Ambient orbs */
  .orb { position: fixed; border-radius: 50%; filter: blur(90px); pointer-events: none; z-index: 0; }
  .orb-1 { width: 600px; height: 600px; background: #3A82FF; top: -200px; left: -150px; opacity: 0.07; }
  .orb-2 { width: 400px; height: 400px; background: #7C3AED; bottom: 5%;  right: -100px; opacity: 0.06; }
  .orb-3 { width: 280px; height: 280px; background: #22C55E; top: 45%;    left: 35%;    opacity: 0.035; }
  header, .stats, .grid { position: relative; z-index: 1; }

  header { padding: 14px 32px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; background: rgba(10,14,23,0.85); backdrop-filter: blur(14px); position: sticky; top: 0; z-index: 100; }
  .logo { display: flex; align-items: center; gap: 12px; }
  .logo h1 { font-size: 1rem; font-weight: 700; letter-spacing: -0.02em; }
  .logo h1 span { color: var(--blue); }
  .tailnet-badge { font-size: 0.63rem; font-weight: 500; color: var(--muted); background: rgba(255,255,255,0.05); border: 1px solid var(--border); padding: 2px 9px; border-radius: 20px; margin-left: 6px; vertical-align: middle; letter-spacing: 0.04em; }
  .header-right { display: flex; align-items: center; gap: 10px; }
  #updated { font-size: 0.68rem; color: var(--muted); }
  .btn { background: rgba(255,255,255,0.05); border: 1px solid var(--border2); color: var(--muted); padding: 6px 14px; border-radius: 20px; cursor: pointer; font-size: 0.73rem; font-weight: 500; transition: all 0.2s; font-family: inherit; }
  .btn:hover { color: var(--text); border-color: rgba(255,255,255,0.28); background: rgba(255,255,255,0.09); }

  .stats { display: flex; border-bottom: 1px solid var(--border); background: rgba(10,14,23,0.5); padding: 0 32px; }
  .stat { padding: 18px 32px 18px 0; border-right: 1px solid var(--border); margin-right: 32px; }
  .stat:last-child { border-right: none; }
  .stat-val { font-size: 2rem; font-weight: 800; line-height: 1; letter-spacing: -0.04em; font-variant-numeric: tabular-nums; }
  .stat-label { font-size: 0.6rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.11em; margin-top: 5px; font-weight: 500; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; padding: 24px 32px; }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px 20px 20px 22px; position: relative; transition: border-color 0.3s, transform 0.3s, box-shadow 0.3s; overflow: hidden; }
  .card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.09), transparent); }
  .card::after  { content: ''; position: absolute; left: 0; top: 20px; bottom: 20px; width: 2px; background: var(--border2); border-radius: 0 2px 2px 0; transition: box-shadow 0.3s; }
  .card.online::after { background: var(--green); box-shadow: 0 0 10px rgba(34,197,94,0.45); }
  .card.self::after   { background: var(--blue);  box-shadow: 0 0 10px rgba(58,130,255,0.45); }
  .card.offline { opacity: 0.52; }
  .card:hover { border-color: var(--border2); transform: translateY(-2px); box-shadow: 0 12px 36px rgba(0,0,0,0.4); }
  .card.online:hover { border-color: rgba(34,197,94,0.2); }
  .card.self:hover   { border-color: rgba(58,130,255,0.22); }

  .card-top { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 12px; }
  .device-left { display: flex; align-items: center; gap: 10px; }
  .os-icon { font-size: 1.35rem; line-height: 1; }
  .device-name { font-size: 0.9rem; font-weight: 600; color: var(--text); letter-spacing: -0.01em; }
  .device-hostname { font-size: 0.66rem; color: var(--muted); font-family: 'SF Mono','Consolas',monospace; margin-top: 2px; }

  .status-pill { display: flex; align-items: center; gap: 5px; font-size: 0.62rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; padding: 3px 9px; border-radius: 20px; white-space: nowrap; border: 1px solid transparent; }
  .pill-online  { background: rgba(34,197,94,0.1);   color: var(--green);  border-color: rgba(34,197,94,0.22); }
  .pill-offline { background: rgba(255,255,255,0.04); color: var(--muted);  border-color: var(--border); }
  .pill-self    { background: rgba(58,130,255,0.1);   color: var(--blue);   border-color: rgba(58,130,255,0.22); }
  .dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
  .pill-online .dot { box-shadow: 0 0 5px currentColor; }
  .pill-self   .dot { box-shadow: 0 0 5px currentColor; }

  .specs { font-size: 0.7rem; color: var(--muted); margin-bottom: 14px; min-height: 14px; line-height: 1.5; }

  .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 14px; }
  .metric { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 10px; padding: 9px 12px; transition: border-color 0.2s; }
  .metric:hover { border-color: var(--border2); }
  .m-label { font-size: 0.59rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.09em; margin-bottom: 4px; font-weight: 500; }
  .m-val { font-size: 0.82rem; font-weight: 600; font-family: 'SF Mono','Consolas',monospace; font-variant-numeric: tabular-nums; }
  .c-green  { color: var(--green); }  .c-yellow { color: var(--yellow); }
  .c-blue   { color: var(--blue); }   .c-muted  { color: var(--muted); }

  .card-actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .act-btn { flex: 1; min-width: 80px; display: flex; align-items: center; justify-content: center; gap: 5px; padding: 7px 10px; border-radius: 20px; border: 1px solid var(--border2); background: rgba(255,255,255,0.04); color: var(--muted); font-size: 0.67rem; font-weight: 600; cursor: pointer; transition: all 0.2s; text-transform: uppercase; letter-spacing: 0.05em; font-family: inherit; }
  .act-btn:hover:not(:disabled)              { border-color: var(--blue);   color: var(--blue);   background: rgba(58,130,255,0.09);  box-shadow: 0 0 14px rgba(58,130,255,0.18); }
  .act-btn.mirror-btn:hover:not(:disabled)   { border-color: var(--purple); color: var(--purple); background: rgba(167,139,250,0.09); box-shadow: 0 0 14px rgba(167,139,250,0.18); }
  .act-btn.power-btn:hover:not(:disabled)    { border-color: var(--yellow); color: var(--yellow); background: rgba(245,158,11,0.09);  box-shadow: 0 0 14px rgba(245,158,11,0.18); }
  .act-btn.shutdown-btn:hover:not(:disabled) { border-color: var(--red);    color: var(--red);    background: rgba(239,68,68,0.09);   box-shadow: 0 0 14px rgba(239,68,68,0.18); }
  .act-btn.wol-btn { border-color: rgba(34,197,94,0.3); color: var(--green); background: rgba(34,197,94,0.07); }
  .act-btn.wol-btn:hover:not(:disabled)      { border-color: var(--green);  color: var(--green);  background: rgba(34,197,94,0.13);   box-shadow: 0 0 14px rgba(34,197,94,0.22); }
  .act-btn:disabled { opacity: 0.28; cursor: not-allowed; }

  #toast { position: fixed; bottom: 28px; right: 28px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
  .toast-msg { background: rgba(17,24,39,0.92); backdrop-filter: blur(14px); border: 1px solid var(--border2); border-radius: 12px; padding: 12px 18px; font-size: 0.77rem; color: var(--text); box-shadow: 0 8px 32px rgba(0,0,0,0.5); animation: toast-in 0.25s cubic-bezier(0.165,0.84,0.44,1); }
  .toast-msg.ok  { border-color: rgba(34,197,94,0.4); }
  .toast-msg.err { border-color: rgba(239,68,68,0.4); }
  @keyframes toast-in { from { opacity:0; transform: translateY(10px) scale(0.97); } to { opacity:1; transform:none; } }

  .self-tag { position: absolute; top: 12px; right: 12px; background: rgba(58,130,255,0.12); color: var(--blue); border: 1px solid rgba(58,130,255,0.25); font-size: 0.57rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; padding: 2px 8px; border-radius: 20px; }

  .loading { text-align: center; padding: 80px 0; color: var(--muted); font-size: 0.88rem; }
  .spin { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border2); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.7s linear infinite; margin-right: 8px; vertical-align: -3px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Terminal modal */
  .modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.72); backdrop-filter: blur(10px); align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; align-items: center; justify-content: center; }
  .term-window { background: rgba(9,13,22,0.96); backdrop-filter: blur(20px); border: 1px solid var(--border2); border-radius: 16px; overflow: hidden; width: min(900px,95vw); height: min(580px,90vh); display: flex; flex-direction: column; box-shadow: 0 32px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04); margin: 0 auto; align-self: center; }
  .term-titlebar { display: flex; align-items: center; justify-content: space-between; padding: 12px 18px; background: rgba(255,255,255,0.03); border-bottom: 1px solid var(--border); flex-shrink: 0; }
  .term-title { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--muted); font-family: 'SF Mono','Consolas',monospace; }
  .term-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border2); transition: box-shadow 0.3s; }
  .term-dot.connecting { background: var(--yellow); box-shadow: 0 0 7px var(--yellow); animation: pulse 1s infinite; }
  .term-dot.connected  { background: var(--green);  box-shadow: 0 0 7px var(--green); }
  .term-dot.closed     { background: var(--red);    box-shadow: 0 0 7px var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .term-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 1rem; padding: 4px 8px; border-radius: 6px; transition: all 0.15s; }
  .term-close:hover { color: var(--red); background: rgba(239,68,68,0.1); }
  #term-container { flex: 1; overflow: hidden; padding: 6px; }
  .term-hint { font-size: 0.64rem; color: var(--muted); text-align: center; padding: 6px; background: rgba(0,0,0,0.3); flex-shrink: 0; letter-spacing: 0.03em; }

  @media (max-width: 640px) { .grid { padding: 14px; gap: 10px; } .stats { padding: 0 16px; } }

  /* Mirror modal */
  .mirror-window { background: rgba(9,13,22,0.96); backdrop-filter: blur(20px); border: 1px solid var(--border2); border-radius: 16px; overflow: hidden; display: flex; flex-direction: column; box-shadow: 0 32px 80px rgba(0,0,0,0.7); max-height: 90vh; }
  .mirror-layout { display: flex; gap: 0; flex: 1; overflow: hidden; }
  .mirror-screen-wrap { flex: 1; display: flex; align-items: center; justify-content: center; background: #000; overflow: hidden; min-width: 0; padding: 12px; }
  #mirror-img { max-height: 70vh; max-width: 100%; border-radius: 8px; cursor: crosshair; display: none; }
  .mirror-placeholder { color: var(--muted); font-size: 0.85rem; text-align: center; padding: 40px; }
  .mirror-sidebar { width: 200px; flex-shrink: 0; border-left: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; padding: 14px; overflow-y: auto; background: rgba(255,255,255,0.02); }
  .mirror-sidebar-title { font-size: 0.61rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.09em; margin-top: 4px; font-weight: 500; }
  .mirror-btn-row { display: flex; gap: 6px; }
  .m-ctrl { flex: 1; background: rgba(255,255,255,0.04); border: 1px solid var(--border2); color: var(--muted); padding: 7px 6px; border-radius: 8px; font-size: 0.72rem; cursor: pointer; text-align: center; transition: all 0.2s; font-family: inherit; }
  .m-ctrl:hover { color: var(--text); border-color: var(--blue); background: rgba(58,130,255,0.08); }
  .m-ctrl.active { background: rgba(58,130,255,0.12); color: var(--blue); border-color: rgba(58,130,255,0.4); }
  .m-ctrl.purple:hover { color: var(--purple); border-color: var(--purple); background: rgba(167,139,250,0.08); }
  .mirror-input { width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border2); color: var(--text); padding: 8px 10px; border-radius: 8px; font-size: 0.8rem; font-family: inherit; }
  .mirror-input:focus { outline: none; border-color: rgba(58,130,255,0.5); box-shadow: 0 0 0 3px rgba(58,130,255,0.1); }
  .mirror-send { width: 100%; background: var(--blue); border: none; color: #fff; padding: 8px; border-radius: 8px; font-size: 0.75rem; font-weight: 600; cursor: pointer; font-family: inherit; transition: all 0.2s; }
  .mirror-send:hover { background: #5a95ff; box-shadow: 0 0 14px rgba(58,130,255,0.35); }
  .mirror-tap-hint { font-size: 0.62rem; color: var(--muted); text-align: center; margin-top: 4px; }
  .mirror-status { font-size: 0.66rem; color: var(--muted); padding: 7px 14px; background: rgba(0,0,0,0.3); border-top: 1px solid var(--border); flex-shrink: 0; display: flex; justify-content: space-between; }
  .auto-refresh-on { color: var(--green) !important; }
</style>
</head>
<body>
<div class="orb orb-1"></div>
<div class="orb orb-2"></div>
<div class="orb orb-3"></div>
<div id="toast"></div>

<header>
  <div class="logo">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--blue)">
      <circle cx="12" cy="12" r="3"/><circle cx="12" cy="3" r="1.5"/><circle cx="12" cy="21" r="1.5"/>
      <circle cx="3" cy="12" r="1.5"/><circle cx="21" cy="12" r="1.5"/>
      <circle cx="5.5" cy="5.5" r="1.5"/><circle cx="18.5" cy="18.5" r="1.5"/>
      <circle cx="18.5" cy="5.5" r="1.5"/><circle cx="5.5" cy="18.5" r="1.5"/>
    </svg>
    <h1>Tail<span>net</span> Dashboard <span class="tailnet-badge" id="tailnet-id">loading…</span></h1>
  </div>
  <div class="header-right">
    <span id="updated"></span>
    <button class="btn" onclick="refresh()">↻ Refresh</button>
  </div>
</header>

<div class="stats">
  <div class="stat"><div class="stat-val" id="s-total">—</div><div class="stat-label">Devices</div></div>
  <div class="stat"><div class="stat-val" style="color:var(--green)" id="s-online">—</div><div class="stat-label">Online</div></div>
  <div class="stat"><div class="stat-val" style="color:var(--muted)" id="s-offline">—</div><div class="stat-label">Offline</div></div>
  <div class="stat"><div class="stat-val" style="color:var(--blue)" id="s-latency">—</div><div class="stat-label">Avg Latency</div></div>
</div>

<div class="grid" id="grid">
  <div class="loading"><span class="spin"></span>Polling devices…</div>
</div>

<!-- Mirror modal -->
<div class="modal-overlay" id="mirror-modal" onclick="maybeCloseMirror(event)">
  <div class="mirror-window" style="width:min(700px,95vw)">
    <div class="term-titlebar">
      <div class="term-title">
        <span class="term-dot" id="mirror-dot"></span>
        <span id="mirror-label">iPhone Mirror</span>
      </div>
      <button class="term-close" onclick="closeMirror()">✕</button>
    </div>
    <div class="mirror-layout">
      <div class="mirror-screen-wrap">
        <img id="mirror-img" alt="iPhone screen" />
        <div class="mirror-placeholder" id="mirror-placeholder">
          Open <strong>iPhone Mirroring</strong> and bring the window to the foreground,<br>then click ⟳ Refresh.
        </div>
      </div>
      <div class="mirror-sidebar">
        <div class="mirror-sidebar-title">Controls</div>
        <div class="mirror-btn-row">
          <button class="m-ctrl" onclick="mirrorAction('home')">⌂ Home</button>
          <button class="m-ctrl" onclick="mirrorAction('switcher')">⊞ Apps</button>
        </div>
        <button class="m-ctrl" onclick="mirrorRefresh()">⟳ Refresh</button>
        <button class="m-ctrl active" id="auto-btn" onclick="toggleAuto()">⏱ Auto: ON</button>

        <div class="mirror-sidebar-title" style="margin-top:8px">Type Text</div>
        <input class="mirror-input" id="mirror-text-input" placeholder="Text to type…"
               onkeydown="if(event.key==='Enter') mirrorType()" />
        <button class="mirror-send" onclick="mirrorType()">Send Text</button>

        <div class="mirror-sidebar-title" style="margin-top:8px">Launch App</div>
        <input class="mirror-input" id="mirror-app-input" placeholder="App name…"
               onkeydown="if(event.key==='Enter') mirrorLaunch()" />
        <button class="mirror-send" onclick="mirrorLaunch()">Launch</button>

        <div class="mirror-tap-hint" style="margin-top:8px">Click screen to tap</div>
      </div>
    </div>
    <div class="mirror-status">
      <span id="mirror-status-text">Ready</span>
      <span id="mirror-ts"></span>
    </div>
  </div>
</div>

<!-- Terminal modal -->
<div class="modal-overlay" id="term-modal" onclick="maybeClose(event)">
  <div class="term-window">
    <div class="term-titlebar">
      <div class="term-title">
        <span class="term-dot" id="term-dot"></span>
        <span id="term-label">Terminal</span>
      </div>
      <button class="term-close" onclick="closeTerminal()">✕</button>
    </div>
    <div id="term-container"></div>
    <div class="term-hint">Tailscale SSH · type <kbd style="background:#21262d;padding:1px 5px;border-radius:3px">exit</kbd> to disconnect</div>
  </div>
</div>

<script>
const OS_EMOJI = { apple:'🍎', windows:'🪟', mobile:'📱', android:'📱', linux:'🐧', device:'💻' };

// ── Terminal (xterm.js loaded lazily) ──────────────────────────────────────
let _ws = null, _term = null, _fit = null, _depsLoaded = false;
let _openingTerminal = false;

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

async function ensureDeps() {
  if (_depsLoaded) return true;
  try {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css';
    document.head.appendChild(css);
    await loadScript('https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js');
    await loadScript('https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js');
    _depsLoaded = true;
    return true;
  } catch(e) {
    alert('Could not load terminal library. Check internet connection.');
    return false;
  }
}

async function openTerminal(ip, username, port, label) {
  try {
    if (_openingTerminal) return;
    _openingTerminal = true;
    if (!await ensureDeps()) return;
    if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
      throw new Error('terminal library not available');
    }

    const portSuffix = port && port != 22 ? ':' + port : '';
    document.getElementById('term-label').textContent = label + ' (' + username + '@' + ip + portSuffix + ')';
    const dot = document.getElementById('term-dot');
    dot.className = 'term-dot connecting';

    const container = document.getElementById('term-container');
    container.innerHTML = '';
    if (_term) { try { _term.dispose(); } catch(e){} }

    _term = new Terminal({
      theme: { background:'#0d1117', foreground:'#e6edf3', cursor:'#58a6ff', selectionBackground:'#264f78' },
      fontFamily: "'SF Mono','Cascadia Code','Consolas',monospace",
      fontSize: 13, lineHeight: 1.25, cursorBlink: true, scrollback: 2000,
    });
    _fit = new FitAddon.FitAddon();
    _term.loadAddon(_fit);
    _term.open(container);
    _fit.fit();

    // Native WebSocket — no socket.io needed
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    _ws = new WebSocket(proto + '://' + location.host + '/ssh');

    _ws.onopen = () => {
      _ws.send(JSON.stringify({ ip, username, port, cols: _term.cols, rows: _term.rows }));
      dot.className = 'term-dot connected';
      _term.focus();
    };
    _ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.t === 'o')      { _term.write(m.d); _term.scrollToBottom(); }
      if (m.t === 'closed') { dot.className = 'term-dot closed'; _term.write('\\r\\n\\x1b[2m[session ended]\\x1b[0m\\r\\n'); }
    };
    _ws.onclose = () => { dot.className = 'term-dot closed'; };
    _ws.onerror = () => { dot.className = 'term-dot closed'; _term.write('\\r\\n\\x1b[31m[connection error]\\x1b[0m\\r\\n'); };

    _term.onData(d => { if (_ws.readyState === 1) _ws.send(JSON.stringify({t:'i', d})); });
    _term.onResize(({cols,rows}) => { if (_ws.readyState === 1) _ws.send(JSON.stringify({t:'r',cols,rows})); });
    new ResizeObserver(() => { if (_fit) _fit.fit(); }).observe(container);

    // Prevent background scroll / focus jumps while modal is open
    document.body.style.overflow = 'hidden';
    document.getElementById('term-modal').classList.add('open');
    setTimeout(() => { _fit.fit(); _term.focus(); }, 80);
  } catch (e) {
    alert('Terminal failed to open: ' + (e && e.message ? e.message : e));
  } finally {
    _openingTerminal = false;
  }
}

function closeTerminal() {
  document.getElementById('term-modal').classList.remove('open');
  document.body.style.overflow = '';
  if (_ws)   { _ws.close(); _ws = null; }
  if (_term) { _term.dispose(); _term = null; }
}

function maybeClose(e) {
  if (e.target === document.getElementById('term-modal')) closeTerminal();
}

// ── Card rendering ─────────────────────────────────────────────────────────
function latClass(ms) {
  if (ms == null) return 'c-muted';
  if (ms < 20)    return 'c-green';
  if (ms < 100)   return '';
  return 'c-yellow';
}

function cardHash(d) {
  return [d.online, d.latency, d.direct, d.relay, d.rx_bytes, d.tx_bytes, d.last_seen, d.last_handshake].join('|');
}

function cardHTML(d) {
  const stateClass = d.is_self ? 'self' : (d.online ? 'online' : 'offline');
  const pillClass  = d.is_self ? 'pill-self' : (d.online ? 'pill-online' : 'pill-offline');
  const pillText   = d.is_self ? 'this machine' : (d.online ? 'online' : 'offline');
  const icon       = OS_EMOJI[d.os_icon] || '💻';

  const latVal = d.latency != null
    ? '<span class="m-val ' + latClass(d.latency) + '">' + d.latency + 'ms</span>'
    : '<span class="m-val c-muted">' + (d.online ? '…' : '—') + '</span>';

  const conn      = d.is_self ? '—' : (d.direct ? 'direct ✓' : (d.relay || 'relay'));
  const connClass = d.direct ? 'c-green' : '';

  const rxTx = d.online
    ? '<div class="metric"><div class="m-label">↓ Recv</div><span class="m-val">' + d.rx_bytes + '</span></div>' +
      '<div class="metric"><div class="m-label">↑ Sent</div><span class="m-val">' + d.tx_bytes + '</span></div>'
    : '<div class="metric"><div class="m-label">Last Seen</div><span class="m-val c-muted">' + (d.last_seen||'—') + '</span></div>' +
      '<div class="metric"><div class="m-label">Handshake</div><span class="m-val c-muted">' + (d.last_handshake||'—') + '</span></div>';

  let actions = '';
  if (d.has_ssh) {
    const dis = !d.online ? ' disabled' : '';
    actions += '<button class="act-btn term-btn"' + dis +
      ' data-ip="' + d.ip + '"' +
      ' data-user="' + d.ssh_user + '"' +
      ' data-port="' + (d.ssh_port || 22) + '"' +
      ' data-label="' + d.label.replace(/"/g, '&quot;') + '"' +
      '>⌨ Terminal</button>';
  }
  if (d.has_power && d.online) {
    const hn = d.hostname; const lbl = d.label.replace(/"/g, '&quot;');
    actions += '<button class="act-btn power-btn" data-hostname="' + hn + '" data-action="restart" data-label="' + lbl + '">↺ Restart</button>';
    actions += '<button class="act-btn power-btn shutdown-btn" data-hostname="' + hn + '" data-action="shutdown" data-label="' + lbl + '">⏻ Shutdown</button>';
  }
  if (!d.online && d.mac && !d.is_self) {
    actions += '<button class="act-btn wol-btn" data-hostname="' + d.hostname + '" data-label="' + d.label.replace(/"/g, '&quot;') + '">⚡ Wake</button>';
  }
  if (d.has_mirror && d.online) {
    actions += '<div style="font-size:0.68rem;color:var(--purple);padding:5px 2px;letter-spacing:0.03em">⬡ AI-controlled · ask Claude</div>';
  }

  return '<div class="card ' + stateClass + '" id="card-' + d.hostname + '" data-hash="' + cardHash(d) + '">' +
    (d.is_self ? '<div class="self-tag">YOU</div>' : '') +
    '<div class="card-top"><div class="device-left"><div class="os-icon">' + icon + '</div><div>' +
    '<div class="device-name">' + d.label + '</div>' +
    '<div class="device-hostname">' + d.hostname + ' · ' + d.ip + '</div></div></div>' +
    '<div class="status-pill ' + pillClass + '"><span class="dot"></span>' + pillText + '</div></div>' +
    '<div class="specs">' + d.specs + '</div>' +
    '<div class="metrics">' +
      '<div class="metric"><div class="m-label">Latency</div>' + latVal + '</div>' +
      '<div class="metric"><div class="m-label">Connection</div><span class="m-val ' + connClass + '">' + conn + '</span></div>' +
      rxTx +
    '</div>' +
    (actions ? '<div class="card-actions">' + actions + '</div>' : '') +
    '</div>';
}

// ── Polling ────────────────────────────────────────────────────────────────
let _knownHostnames = [];

async function refresh() {
  const grid = document.getElementById('grid');
  let d;
  try {
    const r = await fetch('/api/status');
    d = await r.json();
  } catch(e) {
    // Only show error if grid is empty (first load)
    if (!grid.children.length || grid.querySelector('.loading')) {
      grid.innerHTML = '<div class="loading" style="color:var(--red)">Fetch failed: ' + e.message + '</div>';
    }
    return;
  }
  try {
    document.getElementById('tailnet-id').textContent = d.tailnet;
    document.getElementById('updated').textContent    = 'Updated ' + new Date(d.updated).toLocaleTimeString();
    document.getElementById('s-total').textContent    = d.total;
    document.getElementById('s-online').textContent   = d.online;
    document.getElementById('s-offline').textContent  = d.offline;
    document.getElementById('s-latency').textContent  = d.avg_latency != null ? d.avg_latency + 'ms' : '—';

    if (!d.devices || !d.devices.length) {
      if (!grid.children.length || grid.querySelector('.loading')) {
        grid.innerHTML = '<div class="loading" style="color:var(--yellow)">No devices returned</div>';
      }
      return;
    }

    const newHostnames = d.devices.map(dev => dev.hostname);

    // First load or device list changed — do a full render
    const listChanged = newHostnames.join('\\0') !== _knownHostnames.join('\\0');
    if (listChanged || grid.querySelector('.loading')) {
      grid.innerHTML = d.devices.map(cardHTML).join('');
      _knownHostnames = newHostnames;
      return;
    }

    // Soft update: skip cards whose dynamic fields haven't changed
    d.devices.forEach(dev => {
      const existing = document.getElementById('card-' + dev.hostname);
      if (!existing) return;
      const hash = cardHash(dev);
      if (existing.dataset.hash === hash) return;  // nothing changed
      const tmp = document.createElement('div');
      tmp.innerHTML = cardHTML(dev);
      existing.replaceWith(tmp.firstElementChild);
    });
  } catch(e) {
    if (!grid.children.length || grid.querySelector('.loading')) {
      grid.innerHTML = '<div class="loading" style="color:var(--red)">Render error: ' + e.message + '<br><small>' + e.stack + '</small></div>';
    }
  }
}

// ── Mirror ─────────────────────────────────────────────────────────────────
let _autoTimer = null;

function openMirror(label) {
  document.getElementById('mirror-label').textContent = label + ' — iPhone Mirroring';
  document.getElementById('mirror-modal').classList.add('open');
  document.getElementById('mirror-dot').className = 'term-dot connecting';
  const btn = document.getElementById('auto-btn');
  btn.textContent = '⏱ Auto: OFF'; btn.classList.remove('active');
  mirrorRefresh();
}

function closeMirror() {
  document.getElementById('mirror-modal').classList.remove('open');
  clearInterval(_autoTimer); _autoTimer = null;
}

function maybeCloseMirror(e) {
  if (e.target === document.getElementById('mirror-modal')) closeMirror();
}

async function mirrorRefresh() {
  document.getElementById('mirror-status-text').textContent = 'Fetching screenshot…';
  try {
    const r = await fetch('/api/mirror/screenshot');
    if (!r.ok) {
      const e = await r.json();
      document.getElementById('mirror-status-text').textContent = e.error || 'Error';
      document.getElementById('mirror-dot').className = 'term-dot closed';
      return;
    }
    const d = await r.json();
    const img = document.getElementById('mirror-img');
    img.src = 'data:' + d.mime + ';base64,' + d.data;
    img.style.display = 'block';
    document.getElementById('mirror-placeholder').style.display = 'none';
    document.getElementById('mirror-dot').className = 'term-dot connected';
    document.getElementById('mirror-ts').textContent = new Date().toLocaleTimeString();
    document.getElementById('mirror-status-text').textContent = 'Live';
  } catch(e) {
    document.getElementById('mirror-status-text').textContent = 'Fetch failed: ' + e.message;
  }
}

function toggleAuto() {
  const btn = document.getElementById('auto-btn');
  if (_autoTimer) {
    clearInterval(_autoTimer); _autoTimer = null;
    btn.textContent = '⏱ Auto: OFF'; btn.classList.remove('active');
  } else {
    _autoTimer = setInterval(mirrorRefresh, 2500);
    btn.textContent = '⏱ Auto: ON'; btn.classList.add('active');
  }
}

async function mirrorAction(action, extra) {
  document.getElementById('mirror-status-text').textContent = action + '…';
  await fetch('/api/mirror/action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({action}, extra || {}))
  });
  setTimeout(mirrorRefresh, 400);
}

function mirrorType() {
  const t = document.getElementById('mirror-text-input');
  if (t.value) { mirrorAction('type', {text: t.value}); t.value = ''; }
}

function mirrorLaunch() {
  const a = document.getElementById('mirror-app-input');
  if (a.value) { mirrorAction('launch', {app: a.value}); a.value = ''; }
}

// Click-to-tap: maps display coords → actual screenshot coords
document.getElementById('mirror-img').addEventListener('click', function(e) {
  const img  = e.currentTarget;
  const rect = img.getBoundingClientRect();
  const x    = Math.round((e.clientX - rect.left) * (img.naturalWidth  / rect.width));
  const y    = Math.round((e.clientY - rect.top)  * (img.naturalHeight / rect.height));
  mirrorAction('tap', {x, y});
});

function showToast(msg, type = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast-msg ' + type;
  el.textContent = msg;
  document.getElementById('toast').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

async function powerAction(hostname, action, label) {
  const verb = action === 'shutdown' ? 'Shut down' : 'Restart';
  if (!confirm(verb + ' ' + label + '?')) return;
  try {
    const r = await fetch('/api/power', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hostname, action})});
    const d = await r.json();
    if (d.ok) showToast(verb + ' command sent to ' + label);
    else showToast('Error: ' + d.error, 'err');
  } catch(e) { showToast('Request failed: ' + e.message, 'err'); }
}

async function wakeDevice(hostname, label) {
  try {
    const r = await fetch('/api/wol', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hostname})});
    const d = await r.json();
    if (d.ok) showToast('Magic packet sent to ' + label + ' ⚡');
    else showToast('Error: ' + d.error, 'err');
  } catch(e) { showToast('Request failed: ' + e.message, 'err'); }
}

// Event delegation — handles buttons that are re-rendered on every refresh
document.getElementById('grid').addEventListener('click', function(e) {
  const btn = e.target.closest('.term-btn');
  if (btn && !btn.disabled) openTerminal(btn.dataset.ip, btn.dataset.user, btn.dataset.port || 22, btn.dataset.label);
  const mbtn = e.target.closest('.mirror-btn');
  if (mbtn && !mbtn.disabled) openMirror(mbtn.dataset.label);
  const pbtn = e.target.closest('.power-btn');
  if (pbtn && !pbtn.disabled) powerAction(pbtn.dataset.hostname, pbtn.dataset.action, pbtn.dataset.label);
  const wbtn = e.target.closest('.wol-btn');
  if (wbtn && !wbtn.disabled) wakeDevice(wbtn.dataset.hostname, wbtn.dataset.label);
});

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    print(f"\n  Tailnet Dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
