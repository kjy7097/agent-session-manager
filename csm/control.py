"""Control plane — registry + UI host + authenticated proxy to agents.

The browser talks only to this server (no secret in the browser). It serves the
static UI, exposes the machine registry, and proxies /api/m/<machine_id>/<path>
to that machine's agent, attaching the shared bearer secret. In Phase 1 the
only machine is the local agent; Phase 2 adds tailnet peers to the registry.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .config import load_config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# PowerShell run (elevated) on a new Windows machine to authorize this mac + prep.
_ENROLL_PS1 = r"""# CSM enroll — run in an ELEVATED PowerShell on the NEW machine
$ErrorActionPreference='Continue'
$key='__PUBKEY__'
Write-Host '== OpenSSH Server =='
Get-WindowsCapability -Online -Name OpenSSH.Server* 2>$null | Where-Object State -ne Installed | ForEach-Object { Add-WindowsCapability -Online -Name $_.Name | Out-Null }
Set-Service sshd -StartupType Automatic -EA SilentlyContinue; Start-Service sshd -EA SilentlyContinue
Write-Host '== authorize mac key =='
$f="$env:ProgramData\ssh\administrators_authorized_keys"
if(!(Test-Path $f)){ New-Item -ItemType File -Force -Path $f | Out-Null }
if(-not (Select-String -Path $f -SimpleMatch $key -Quiet)){ Add-Content -Path $f -Value $key -Encoding ascii }
icacls $f /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
Write-Host '== python check =='
$py=(Get-Command python.exe -All -EA SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1).Source
if($py){ Write-Host "python OK: $py" } else { Write-Host 'WARNING: real Python not found — install from https://python.org first' }
$ip=(Get-NetIPAddress -AddressFamily IPv4 -EA SilentlyContinue | Where-Object { $_.IPAddress -like '100.*' } | Select-Object -First 1).IPAddress
Write-Host ''
Write-Host "DONE  tailnetIP=$ip  hostname=$env:COMPUTERNAME"
Write-Host '이제 맥 웹UI에서 이 노드 옆 [배포] 버튼을 누르세요.'
"""

# Bash run on a new LINUX machine (sudo prompts) to authorize this host + prep.
_ENROLL_SH = r"""# CSM enroll — run on the NEW Linux machine (sudo will prompt)
key='__PUBKEY__'
echo '== OpenSSH server =='
if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y openssh-server >/dev/null
elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y openssh-server >/dev/null
elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm openssh >/dev/null; fi
sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd 2>/dev/null || true
echo '== authorize control host key =='
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
grep -qF "$key" "$HOME/.ssh/authorized_keys" 2>/dev/null || echo "$key" >> "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
echo '== python check =='
command -v python3 >/dev/null 2>&1 && echo "python3 OK: $(command -v python3)" || echo 'WARNING: python3 missing — apt/dnf install python3'
ip=$(ip -4 addr 2>/dev/null | grep -oE '100\.[0-9.]+' | head -1)
echo ''
echo "DONE  tailnetIP=${ip:-?}  hostname=$(hostname)  user=$USER"
echo 'Now register this machine in the web UI (Add machine) — deploy auto-detects Linux.'
"""


def build_registry(cfg: dict) -> list[dict]:
    """Registry from config: the local machine (control host) + each entry in
    config['machines'] (registered by host + SSH user, reached via a local SSH
    tunnel on its localPort). No dependency on Tailscale — host can be any IP or
    hostname the control machine can SSH to."""
    agent_port = cfg["agent"]["port"]
    self_name = cfg.get("selfName") or socket.gethostname().split(".")[0]
    machines = [{
        "id": cfg.get("selfId", "local"), "name": self_name, "os": "macos",
        "host": "localhost", "online": True, "is_self": True, "agentReady": True,
        "baseUrl": f"http://127.0.0.1:{agent_port}",
    }]
    for m in cfg.get("machines", []):
        if not m.get("id") or not m.get("localPort"):
            continue
        machines.append({
            "id": m["id"], "name": m.get("name", m["id"]), "os": m.get("os", "linux"),
            "host": m.get("host"), "online": True, "is_self": False, "agentReady": True,
            "baseUrl": f"http://127.0.0.1:{m['localPort']}",
        })
    return machines


def _make_handler(cfg: dict):
    registry = {"by_id": {}, "cfg": cfg}

    def refresh():
        c = load_config()  # re-read so newly deployed agents appear w/o a restart
        registry["cfg"] = c
        machines = build_registry(c)
        registry["by_id"] = {m["id"]: m for m in machines}
        return machines

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            try:
                if sys.stderr:
                    sys.stderr.write("control %s - %s\n" % (self.address_string(), fmt % args))
            except Exception:
                pass

        # ---- static + api routing --------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if u.path == "/api/machines":
                return self._json({"machines": refresh()})
            if u.path == "/enroll":
                return self._enroll()
            if u.path == "/api/manager":
                return self._manager()
            if u.path.startswith("/api/m/"):
                return self._proxy("GET")
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/deploy":
                return self._deploy()
            if p == "/api/machines/add":
                return self._m_add()
            if p == "/api/machines/rename":
                return self._m_rename()
            if p == "/api/machines/remove":
                return self._m_remove()
            if p.startswith("/api/m/"):
                return self._proxy("POST")
            return self._json({"error": "not found"}, 404)

        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except json.JSONDecodeError:
                return None

        def _save_cfg(self, cfg):
            from .config import config_path
            config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            registry["cfg"] = cfg

        def _m_add(self):
            import re as _re
            import secrets as _secrets
            b = self._read_body() or {}
            host, user = (b.get("host") or "").strip(), (b.get("sshUser") or "").strip()
            name = (b.get("name") or host).strip()
            if not host or not user:
                return self._json({"error": "host + sshUser required"}, 400)
            cfg = load_config()
            machines = cfg.setdefault("machines", [])
            base = _re.sub(r"[^a-z0-9-]+", "-", (b.get("id") or name or host).lower()).strip("-") or "machine"
            mid, i = base, 2
            taken = {m["id"] for m in machines} | {cfg.get("selfId", "local")}
            while mid in taken:
                mid, i = f"{base}-{i}", i + 1
            used = {m.get("localPort") for m in machines}
            port = next(p for p in range(9101, 9200) if p not in used)
            machines.append({"id": mid, "name": name, "host": host, "sshUser": user,
                             "sshPort": int(b.get("sshPort") or 22), "os": b.get("os", "windows"),
                             "agentPort": 8766, "localPort": port, "secret": _secrets.token_hex(32)})
            self._save_cfg(cfg)
            log = Path.home() / ".csm-run" / f"deploy-{mid}.log"
            script = Path(__file__).resolve().parent.parent / "scripts" / "deploy_agent.py"
            subprocess.Popen([sys.executable, str(script), mid, host, user, "",
                              str(int(b.get("sshPort") or 22))],
                             stdout=open(log, "wb"), stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
            refresh()
            return self._json({"ok": True, "id": mid, "deploying": True, "log": str(log)})

        def _m_rename(self):
            b = self._read_body() or {}
            mid, name = b.get("id"), (b.get("name") or "").strip()
            if not mid or not name:
                return self._json({"error": "id + name required"}, 400)
            cfg = load_config()
            if mid == cfg.get("selfId", "local"):
                cfg["selfName"] = name
            else:
                for m in cfg.get("machines", []):
                    if m.get("id") == mid:
                        m["name"] = name
                        break
                else:
                    return self._json({"error": "unknown machine"}, 404)
            self._save_cfg(cfg)
            refresh()
            return self._json({"ok": True})

        def _m_remove(self):
            b = self._read_body() or {}
            mid = b.get("id")
            cfg = load_config()
            cfg["machines"] = [m for m in cfg.get("machines", []) if m.get("id") != mid]
            self._save_cfg(cfg)
            import os as _os
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_os.getuid()}/com.csm.tunnels"],
                           capture_output=True)
            refresh()
            return self._json({"ok": True})

        def _manager(self):
            # open/continue (or, with ?fresh=1, reset) the persistent orchestration
            # manager on the control host
            cfg = load_config()
            import os as _os
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            fresh = qs.get("fresh", ["0"])[0] in ("1", "true")
            delete = qs.get("delete", ["0"])[0] in ("1", "true")
            mdir = _os.path.expanduser("~/.csm-manager")
            target = f"http://127.0.0.1:{cfg['agent']['port']}/manager"
            body = json.dumps({"cwd": mdir, "fresh": fresh, "delete": delete}).encode()
            req = urllib.request.Request(target, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {cfg['secret']}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data, status = resp.read(), resp.status
            except urllib.error.URLError as e:
                return self._json({"error": f"manager agent unreachable: {e.reason}"}, 502)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _enroll(self):
            try:
                pub = (Path.home() / ".ssh/id_ed25519.pub").read_text(encoding="utf-8").strip()
            except OSError:
                return self._json({"error": "no mac pubkey"}, 500)
            from urllib.parse import parse_qs
            target_os = parse_qs(urlparse(self.path).query).get("os", ["windows"])[0]
            tmpl = _ENROLL_SH if target_os == "linux" else _ENROLL_PS1
            script = tmpl.replace("__PUBKEY__", pub)
            data = script.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _deploy(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            name = body.get("name")
            refresh()
            entry = next((mm for mm in registry["cfg"].get("machines", []) if mm.get("id") == name), None)
            if not entry or not entry.get("host") or not entry.get("sshUser"):
                return self._json({"error": f"machine '{name}' needs host+sshUser in config"}, 400)
            log = Path.home() / ".csm-run" / f"deploy-{name}.log"
            script = Path(__file__).resolve().parent.parent / "scripts" / "deploy_agent.py"
            args = [sys.executable, str(script), name, entry["host"], entry["sshUser"]]
            if entry.get("pythonExe"):
                args.append(entry["pythonExe"])
            subprocess.Popen(args, stdout=open(log, "wb"), stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
            return self._json({"ok": True, "started": name, "log": str(log)})

        def do_DELETE(self):
            if urlparse(self.path).path.startswith("/api/m/"):
                return self._proxy("DELETE")
            return self._json({"error": "not found"}, 404)

        # ---- proxy -----------------------------------------------------
        def _proxy(self, method):
            # /api/m/<machine_id>/<rest...>?<query>
            u = urlparse(self.path)
            parts = u.path[len("/api/m/"):].split("/", 1)
            mid = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            m = registry["by_id"].get(mid)
            if m is None:
                refresh()
                m = registry["by_id"].get(mid)
            if not m:
                return self._json({"error": f"unknown machine {mid}"}, 404)
            if not m.get("agentReady") or not m.get("baseUrl"):
                return self._json(
                    {"error": f"'{m['name']}' 에 에이전트가 아직 배포되지 않았습니다 (Phase 2)"}, 503
                )
            # per-machine secret from config['machines']; self/local uses cfg['secret']
            agent_secret = registry["cfg"]["secret"]
            for mm in registry["cfg"].get("machines", []):
                if mm.get("id") == mid and mm.get("secret"):
                    agent_secret = mm["secret"]
                    break
            target = m["baseUrl"].rstrip("/") + "/" + rest
            if u.query:
                target += "?" + u.query
            body = None
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                body = self.rfile.read(n)
            req = urllib.request.Request(target, data=body, method=method)
            req.add_header("Authorization", f"Bearer {agent_secret}")
            if body is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    status = resp.status
            except urllib.error.HTTPError as e:
                data = e.read()
                status = e.code
            except urllib.error.URLError as e:
                return self._json({"error": f"agent unreachable: {e.reason}"}, 502)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- helpers ---------------------------------------------------
        def _json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_static(self, name):
            f = WEB_DIR / name
            if not f.is_file():
                return self._json({"error": "ui not found"}, 404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main():
    cfg = load_config()
    host = cfg["control"]["host"]
    port = int(cfg["control"]["port"])
    httpd = ThreadingHTTPServer((host, port), _make_handler(cfg))
    n = len(build_registry(cfg))
    sys.stderr.write(
        f"csm-control {__version__} on http://{host}:{port}  ({n} machine(s))\n"
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
