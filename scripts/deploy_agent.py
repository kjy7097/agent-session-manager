#!/usr/bin/env python3
"""Deploy/refresh a CSM agent over SSH, end-to-end (Windows or Linux target).

Pushes the agent code, writes the agent config (reusing an existing per-machine
secret if already registered), installs an autostart agent (Windows scheduled
task / Linux systemd user service — survives SSH disconnect + reboot), records
the machine in the control-plane config, and nudges the tunnel manager.

Target OS is auto-detected via `uname -s` (Linux/Darwin) else assumed Windows.

Prereq: the control machine's SSH public key is authorized on the target (run
the enroll step there once). The control plane reaches the agent via an SSH
tunnel (csm.tunnels) on the allocated localPort.

Usage: python scripts/deploy_agent.py <id> <host> <sshUser> [pythonExe] [sshPort]
"""
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
RUN = HOME / ".csm-run"
LA = HOME / "Library/LaunchAgents"
CFG = REPO / "csm.config.json"


def ssh(user, host, port, cmd):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                           "-o", "ConnectTimeout=12", "-p", str(port), f"{user}@{host}", cmd],
                          capture_output=True, text=True, errors="replace")


def scp(src, user, host, port, dst):
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                    "-P", str(port), str(src), f"{user}@{host}:{dst}"], check=True)


def detect_os(user, host, port):
    r = ssh(user, host, port, "uname -s")
    s = (r.stdout or "").strip()
    if r.returncode == 0 and s in ("Linux", "Darwin"):
        return "linux" if s == "Linux" else "macos"
    return "windows"


# ---- Windows target (scheduled task) ---------------------------------------

def win_prepare(user, host, port, pyexe):
    if not pyexe:
        r = ssh(user, host, port,
                'powershell -NoProfile -Command "(Get-Command python.exe -All -EA SilentlyContinue | '
                "Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1).Source\"")
        pyexe = (r.stdout or "").strip()
    if not pyexe:
        sys.exit("No real Python found on target (only the Store stub) — install Python first")
    home_dir = (ssh(user, host, port, "echo %USERPROFILE%").stdout or "").strip() or f"C:\\Users\\{user}"
    claude_path = f"{home_dir}\\.local\\bin\\claude.exe"
    ssh(user, host, port, f'"{pyexe}" -m pip install --quiet --user pywinpty')
    ssh(user, host, port, "if not exist csm mkdir csm")
    return pyexe, claude_path, home_dir


def win_autostart(user, host, port, pyexe, home_dir):
    (RUN / "_dep.vbs").write_text(
        f'CreateObject("WScript.Shell").Run "cmd /c cd /d {home_dir} && ""{pyexe}"" -m csm.agent", 0, True\r\n')
    scp(RUN / "_dep.vbs", user, host, port, "run-agent.vbs")
    ps = (
        f"$a=New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '{home_dir}\\run-agent.vbs';"
        "$t=New-ScheduledTaskTrigger -AtLogOn;"
        "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        "Register-ScheduledTask -TaskName 'CSM-Agent' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
        "Get-NetTCPConnection -LocalPort 8766 -State Listen -EA SilentlyContinue | "
        "%{ Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue };"
        "Start-Sleep 1; Start-ScheduledTask -TaskName 'CSM-Agent'; Start-Sleep 4;"
        "Write-Host ('LISTEN='+((Get-NetTCPConnection -LocalPort 8766 -State Listen -EA SilentlyContinue|Measure-Object).Count))"
    )
    (RUN / "_dep.ps1").write_text(ps)
    scp(RUN / "_dep.ps1", user, host, port, "setup-task.ps1")
    out = ssh(user, host, port, f'powershell -NoProfile -ExecutionPolicy Bypass -File {home_dir}\\setup-task.ps1')
    print("task:", (out.stdout or out.stderr or "?").strip().splitlines()[-1:])


# ---- Linux target (systemd user service) -----------------------------------

def linux_prepare(user, host, port, pyexe):
    if not pyexe:
        r = ssh(user, host, port, "command -v python3 || command -v python")
        pyexe = (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else ""
    if not pyexe:
        sys.exit("python3 not found on target — install it first (apt/dnf install python3)")
    r = ssh(user, host, port, "command -v claude || echo $HOME/.local/bin/claude")
    claude_path = (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else "claude"
    home_dir = (ssh(user, host, port, "echo $HOME").stdout or "").strip() or f"/home/{user}"
    ssh(user, host, port, "mkdir -p csm")
    return pyexe, claude_path, home_dir


def linux_autostart(user, host, port, pyexe, home_dir):
    # systemd *user* service + linger so it runs at boot without an interactive login
    unit = (
        "[Unit]\n"
        "Description=CSM Agent\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={pyexe} -m csm.agent\n"
        f"WorkingDirectory={home_dir}\n"
        "Restart=always\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    (RUN / "_dep.service").write_text(unit)
    scp(RUN / "_dep.service", user, host, port, "csm-agent.service")
    setup = (
        "set -e\n"
        'mkdir -p "$HOME/.config/systemd/user"\n'
        'mv -f "$HOME/csm-agent.service" "$HOME/.config/systemd/user/csm-agent.service"\n'
        'loginctl enable-linger "$USER" 2>/dev/null || true\n'
        "export XDG_RUNTIME_DIR=/run/user/$(id -u)\n"
        "systemctl --user daemon-reload\n"
        "systemctl --user enable --now csm-agent 2>&1 || true\n"
        "systemctl --user restart csm-agent 2>&1 || true\n"
        "sleep 4\n"
        "if command -v ss >/dev/null 2>&1; then L=$(ss -ltn 2>/dev/null | grep -c ':8766 '); "
        "else L=$(netstat -ltn 2>/dev/null | grep -c ':8766 '); fi\n"
        'echo "LISTEN=$L"\n'
    )
    (RUN / "_dep_setup.sh").write_text(setup)
    scp(RUN / "_dep_setup.sh", user, host, port, "_csm_setup.sh")
    out = ssh(user, host, port, 'bash "$HOME/_csm_setup.sh"; rm -f "$HOME/_csm_setup.sh"')
    print("service:", (out.stdout or out.stderr or "?").strip().splitlines()[-1:])


def main():
    if len(sys.argv) < 4:
        sys.exit("usage: deploy_agent.py <id> <host> <sshUser> [pythonExe] [sshPort]")
    mid, host, user = sys.argv[1], sys.argv[2], sys.argv[3]
    pyexe = sys.argv[4] if len(sys.argv) > 4 else None
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 22
    RUN.mkdir(exist_ok=True)

    if ssh(user, host, port, "echo ok").returncode != 0:
        sys.exit(f"SSH failed: {user}@{host}:{port} — run the enroll step on the target first")

    osname = detect_os(user, host, port)
    print("target OS:", osname)
    if osname == "windows":
        pyexe, claude_path, home_dir = win_prepare(user, host, port, pyexe)
    elif osname == "linux":
        pyexe, claude_path, home_dir = linux_prepare(user, host, port, pyexe)
    else:
        sys.exit(f"unsupported target OS: {osname} (windows/linux only)")
    print("python:", pyexe)

    cfg = json.loads(CFG.read_text())
    machines = cfg.setdefault("machines", [])
    entry = next((m for m in machines if m.get("id") == mid), None)
    if entry and entry.get("secret"):
        secret, localport = entry["secret"], entry["localPort"]
        print(f"reusing existing registration: localPort {localport}")
    else:
        secret = secrets.token_hex(32)
        used = {m.get("localPort") for m in machines}
        localport = next(p for p in range(9101, 9200) if p not in used)
        print(f"new registration: localPort {localport}")

    # push code (csm/*.py + run_agent.py) — shared
    for f in sorted((REPO / "csm").glob("*.py")):
        scp(f, user, host, port, "csm/")
    scp(REPO / "scripts/run_agent.py", user, host, port, "run_agent.py")

    # agent config — shared (only claudePath differs by OS)
    acfg = {"secret": secret, "claudePath": claude_path,
            "agent": {"host": "127.0.0.1", "port": 8766}, "control": {"host": "127.0.0.1", "port": 8765}}
    (RUN / "_dep.config.json").write_text(json.dumps(acfg, indent=2))
    scp(RUN / "_dep.config.json", user, host, port, "csm.config.json")

    # autostart — OS-specific
    if osname == "windows":
        win_autostart(user, host, port, pyexe, home_dir)
    else:
        linux_autostart(user, host, port, pyexe, home_dir)

    # record machine in config (control re-reads per request; tunnels picks it up)
    if entry is None:
        entry = {"id": mid}
        machines.append(entry)
    entry.update({"name": entry.get("name", mid), "host": host, "sshUser": user, "sshPort": port,
                  "os": osname, "agentPort": 8766, "localPort": localport,
                  "secret": secret, "pythonExe": pyexe, "claudePath": claude_path})
    CFG.write_text(json.dumps(cfg, indent=2))

    # nudge the tunnel manager to pick up the (new) machine
    plist = LA / "com.csm.tunnels.plist"
    if plist.exists():
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{__import__('os').getuid()}/com.csm.tunnels"], capture_output=True)
    time.sleep(3)
    print(f"✅ deployed '{mid}' ({user}@{host}, {osname}) -> 127.0.0.1:{localport}")


if __name__ == "__main__":
    main()
