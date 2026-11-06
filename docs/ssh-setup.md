# SSH and Wake-on-LAN setup

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

### Android (Termux)
```bash
~/.ssh/authorized_keys   # Termux home
# sshd runs on port 8022 — start with: sshd
```

---

## Wake-on-LAN Setup

Add MAC addresses to `DEVICE_MACS` in `dashboard.py` to enable the WoL button per device:

```python
DEVICE_MACS = {
    "my-desktop": "aa:bb:cc:dd:ee:ff",
    "server-1":   "11:22:33:44:55:66",
}
```

Find a MAC address after pinging the device:
```bash
arp -a         # macOS / Linux
arp -a         # Windows (cmd)
```

---
