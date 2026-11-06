# Tailnet Dashboard

A self-hosted Flask dashboard for a personal Tailscale network. It shows device
status, opens browser SSH terminals, runs remote commands, and provides
Wake-on-LAN and power controls. An optional stdio MCP server exposes device
listing and command execution to compatible clients.

The dashboard runs as a single Python application. It stores command/chat
history in a local SQLite database; it does not require a separate database
service or frontend build.

## Setup

Install Python and Tailscale, sign the host into your tailnet, then:

```sh
git clone https://github.com/all3n1k/tailscale-dashboard.git
cd tailscale-dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.json.example config.json
```

On Windows, activate the environment with `.venv\Scripts\activate`.
Edit `config.json` with your hostnames, SSH users/ports, display labels, and
Wake-on-LAN addresses. The populated file and local history database are ignored
by Git.

Run the dashboard:

```sh
python3 dashboard.py
```

Open http://localhost:5555. Set `PORT` to use another port. The repository also
contains `start.command` and `start.bat` convenience launchers.

This application can run commands and power off machines. Use it on a trusted
local network or private tailnet, not as an unauthenticated public web service.

## Capabilities

| Area | Implementation |
| --- | --- |
| Device overview | Tailscale status, online/offline state, traffic, connection type and latency |
| Interactive terminals | Browser WebSocket terminal with a Unix PTY or Windows Paramiko channel |
| Remote operations | Command palette, SSH execution, restart and shutdown |
| Wake-on-LAN | Magic packets using configured device MAC addresses |
| iPhone controls | Optional macOS Mirroring integration through Mirroir MCP |
| MCP | Device listing, remote command execution, and local command execution |

SSH keys must already be authorized on the target machines.
[SSH and Wake-on-LAN setup](docs/ssh-setup.md) includes Windows administrator
key-file details and Termux port configuration.

## MCP server

Run `python3 /absolute/path/to/tailscale-dashboard/mcp_server.py` as the
stdio command in a compatible MCP client. For example:

```sh
python3 /absolute/path/to/tailscale-dashboard/mcp_server.py
```

Available tools are `tailnet_list_devices`, `tailnet_run`, and
`tailnet_run_local`. SSH user/port overrides come from `config.json`.
Restart the client/server process after changing its code or configuration.

## Source layout

- `dashboard.py`: Flask routes, WebSocket terminals, storage, and inline UI.
- `mcp_server.py`: stdio MCP server.
- `config.json.example`: configuration template.
- `requirements.txt`: Python dependencies.
- [Architecture and API reference](docs/architecture.md).

The dashboard remains a single-file application; modularization and deployment
hardening are areas for further work.
