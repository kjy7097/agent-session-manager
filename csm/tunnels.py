"""Maintain SSH tunnels to every registered machine.

For each entry in config['machines'], keeps a forward-only SSH tunnel
(localPort -> host:agentPort) alive with auto-reconnect, so the control plane
reaches each remote agent at http://127.0.0.1:<localPort>. Uses SSH key auth
(no Tailscale dependency — host is any SSH-reachable IP/hostname).

Run via a single launchd job: python -m csm.tunnels
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from .config import load_config


def _tunnel_loop(m: dict):
    target = f"{m.get('sshUser','')}@{m['host']}" if m.get("sshUser") else m["host"]
    while True:
        try:
            subprocess.run([
                "ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=10", "-p", str(m.get("sshPort", 22)),
                "-N", "-L", f"{m['localPort']}:127.0.0.1:{m.get('agentPort', 8766)}", target,
            ])
        except Exception as e:
            sys.stderr.write(f"[tunnels] {m.get('id')} error: {e}\n")
        time.sleep(3)


def main():
    cfg = load_config()
    machines = [m for m in cfg.get("machines", []) if m.get("host") and m.get("localPort")]
    if not machines:
        sys.stderr.write("[tunnels] no machines configured; idling\n")
    for m in machines:
        threading.Thread(target=_tunnel_loop, args=(m,), daemon=True).start()
        sys.stderr.write(f"[tunnels] {m['id']} -> {m['host']}:{m.get('agentPort',8766)} on :{m['localPort']}\n")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
