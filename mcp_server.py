#!/usr/bin/env python3
"""
Tailnet SSH MCP Server
Exposes your Tailscale network to Claude as callable tools.

Register with:
  claude mcp add --transport stdio tailnet-ssh -- python3 /Users/neo/tailscale-dashboard/mcp_server.py
"""
import json
import sys
import subprocess
import os
import time
from datetime import datetime, timezone

# ─── SSH user mapping (must match dashboard.py) ───────────────────────────────
SSH_USERS = {
    "neo-mac":      "neo",
    "digitalstorm": "ALLEN",
    "neo":          "allen",
    "michellepc":   "michelle",
    "a-pad":        "allen",
}
SSH_DEFAULT_USER = "neo"
SSH_TIMEOUT      = 30   # seconds per remote command

# Some peers (e.g. Termux sshd) run SSH on a non-22 port.
SSH_PORTS = {
    "galaxy-tab-a7-lite": 8022,
}

# ─── Tailscale status cache ───────────────────────────────────────────────────
_TS_TTL    = 3  # seconds
_ts_cache  = {"data": None, "at": 0.0}


# ─── Tool definitions ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "tailnet_list_devices",
        "description": (
            "List all devices on the Tailscale network with their online status, "
            "IP address, OS, and last-seen time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "online_only": {
                    "type": "boolean",
                    "description": "If true, return only currently online devices.",
                    "default": False,
                }
            },
        },
    },
    {
        "name": "tailnet_run",
        "description": (
            "Run a shell command on a remote machine over Tailscale SSH and return stdout/stderr. "
            "Works on macOS and Linux hosts. Windows hosts require OpenSSH Server to be installed. "
            "The command runs non-interactively with a 30-second timeout."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": "Tailscale hostname (e.g. 'digitalstorm', 'neo-mac').",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on the remote machine.",
                },
                "username": {
                    "type": "string",
                    "description": "SSH username. Defaults to the configured user for that host.",
                },
            },
            "required": ["hostname", "command"],
        },
    },
    {
        "name": "tailnet_run_local",
        "description": (
            "Run a shell command on THIS machine (neo-mac) and return the output. "
            "Use for local tasks, file operations, or orchestrating things from the hub."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute locally.",
                },
            },
            "required": ["command"],
        },
    },
]


# ─── Tool implementations ─────────────────────────────────────────────────────

def _tailscale_status():
    now = time.monotonic()
    if _ts_cache["data"] is not None and now - _ts_cache["at"] < _TS_TTL:
        return _ts_cache["data"]
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
    _ts_cache["data"] = data
    _ts_cache["at"]   = now
    return data


def tool_tailnet_list_devices(online_only=False):
    data  = _tailscale_status()
    nodes = []

    def add(node, is_self=False):
        dns      = node.get("DNSName", "")
        hostname = (
            dns.split(".")[0].lower()
            if dns
            else node.get("HostName", "").lower().replace(" ", "-")
        )
        online   = True if is_self else node.get("Online", False)
        if online_only and not online:
            return
        ts = node.get("LastSeen", "")
        nodes.append({
            "hostname":  hostname,
            "ip":        (node.get("TailscaleIPs") or [""])[0],
            "os":        node.get("OS", "unknown"),
            "online":    online,
            "is_self":   is_self,
            "last_seen": ts if ts and not ts.startswith("0001") else "now",
            "relay":     node.get("Relay", ""),
        })

    add(data.get("Self", {}), is_self=True)
    for peer in data.get("Peer", {}).values():
        add(peer)

    nodes.sort(key=lambda n: (not n["online"], n["hostname"]))
    lines = [f"{'HOST':<22} {'IP':<16} {'OS':<8} {'STATUS':<8} RELAY"]
    lines.append("─" * 65)
    for n in nodes:
        status = "self" if n["is_self"] else ("online" if n["online"] else "offline")
        lines.append(f"{n['hostname']:<22} {n['ip']:<16} {n['os']:<8} {status:<8} {n['relay']}")
    return "\n".join(lines)


def _build_node_map(data):
    """Return hostname → node dict from a tailscale status blob."""
    nodes = {}
    for node in [data.get("Self", {})] + list(data.get("Peer", {}).values()):
        dns  = node.get("DNSName", "")
        name = (
            dns.split(".")[0].lower()
            if dns
            else node.get("HostName", "").lower().replace(" ", "-")
        )
        nodes[name] = node
    return nodes


def tool_tailnet_run(hostname, command, username=None):
    data     = _tailscale_status()
    node_map = _build_node_map(data)
    key      = hostname.lower().replace(" ", "-")
    node     = node_map.get(key)

    if not node:
        return f"Error: hostname '{hostname}' not found in tailnet."

    ips    = node.get("TailscaleIPs") or []
    ip     = ips[0] if ips else None
    online = node.get("Online", True)  # Self is always online
    if not online:
        return f"Error: {hostname} is currently offline."
    if not ip:
        return f"Error: hostname '{hostname}' has no Tailscale IP."

    user = username or SSH_USERS.get(hostname.lower(), SSH_DEFAULT_USER)
    port = SSH_PORTS.get(hostname.lower(), 22)
    try:
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
            "-o", "BatchMode=yes",
        ]
        if port and int(port) != 22:
            ssh_cmd += ["-p", str(port)]
        ssh_cmd += [f"{user}@{ip}", command]
        r = subprocess.run(
            ssh_cmd,
            capture_output=True, text=True, timeout=SSH_TIMEOUT,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        parts = []
        if out: parts.append(out)
        if err: parts.append(f"[stderr]\n{err}")
        if r.returncode != 0 and not parts:
            parts.append(f"[exit code {r.returncode}]")
        return "\n".join(parts) or "[no output]"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {SSH_TIMEOUT}s"
    except Exception as e:
        return f"Error: {e}"


def tool_tailnet_run_local(command):
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=SSH_TIMEOUT
        )
        out  = r.stdout.strip()
        err  = r.stderr.strip()
        parts = []
        if out: parts.append(out)
        if err: parts.append(f"[stderr]\n{err}")
        if r.returncode != 0 and not parts:
            parts.append(f"[exit code {r.returncode}]")
        return "\n".join(parts) or "[no output]"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {SSH_TIMEOUT}s"
    except Exception as e:
        return f"Error: {e}"


TOOL_FNS = {
    "tailnet_list_devices": lambda args: tool_tailnet_list_devices(**args),
    "tailnet_run":          lambda args: tool_tailnet_run(**args),
    "tailnet_run_local":    lambda args: tool_tailnet_run_local(**args),
}


# ─── JSON-RPC 2.0 / MCP server loop ──────────────────────────────────────────

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method", "")
    rid    = req.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tailnet-ssh", "version": "1.0.0"},
        }})

    elif method == "notifications/initialized":
        pass  # no response needed

    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})

    elif method == "tools/call":
        params = req.get("params", {})
        name   = params.get("name", "")
        args   = params.get("arguments", {})
        fn     = TOOL_FNS.get(name)
        if fn:
            try:
                result = fn(args)
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": str(result)}]
                }})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"Tool error: {e}"}],
                    "isError": True,
                }})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Unknown tool: {name}"
            }})
    else:
        if rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Method not found: {method}"
            }})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(req)


if __name__ == "__main__":
    main()
