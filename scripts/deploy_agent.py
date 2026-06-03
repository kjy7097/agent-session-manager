#!/usr/bin/env python3
"""Deploy/refresh a Windows CSM agent over SSH, end-to-end.

Pushes the agent code, installs pywinpty, writes the agent config (reusing an
existing per-machine secret if the machine is already registered), registers a
scheduled-task agent (survives SSH disconnect + reboot), records the machine in
the control-plane config (config['machines']), and nudges the tunnel manager.

Prereq: the control machine's SSH public key is authorized on the target (run
the enroll script there once). The control plane reaches the agent via an SSH
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
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                           "-p", str(port), f"{user}@{host}", cmd], capture_output=True, text=True)


def scp(src, user, host, port, dst):
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-P", str(port),
                    str(src), f"{user}@{host}:{dst}"], check=True)


def main():
    if len(sys.argv) < 4:
        sys.exit("usage: deploy_agent.py <id> <host> <sshUser> [pythonExe] [sshPort]")
    mid, host, user = sys.argv[1], sys.argv[2], sys.argv[3]
    pyexe = sys.argv[4] if len(sys.argv) > 4 else None
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 22
    RUN.mkdir(exist_ok=True)

    if ssh(user, host, port, "echo ok").returncode != 0:
        sys.exit(f"SSH 실패: {user}@{host}:{port} — 대상에서 enroll 스크립트로 키 등록 먼저")

    if not pyexe:
        r = ssh(user, host, port,
                'powershell -NoProfile -Command "(Get-Command python.exe -All -EA SilentlyContinue | '
                "Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1).Source\"")
        pyexe = (r.stdout or "").strip()
    if not pyexe:
        sys.exit("대상에 (Store 스텁이 아닌) Python 이 없음 — 먼저 설치 필요")
    print("python:", pyexe)
    home_dir = (ssh(user, host, port, "echo %USERPROFILE%").stdout or "").strip() or f"C:\\Users\\{user}"
    claude_path = f"{home_dir}\\.local\\bin\\claude.exe"
    ssh(user, host, port, f'"{pyexe}" -m pip install --quiet --user pywinpty')

    cfg = json.loads(CFG.read_text())
    machines = cfg.setdefault("machines", [])
    entry = next((m for m in machines if m.get("id") == mid), None)
    if entry and entry.get("secret"):
        secret, localport = entry["secret"], entry["localPort"]
        print(f"기존 등록 재사용: localPort {localport}")
    else:
        secret = secrets.token_hex(32)
        used = {m.get("localPort") for m in machines}
        localport = next(p for p in range(9101, 9200) if p not in used)
        print(f"신규 등록: localPort {localport}")

    # push code + launcher
    ssh(user, host, port, "if not exist csm mkdir csm")
    for f in sorted((REPO / "csm").glob("*.py")):
        scp(f, user, host, port, "csm/")
    scp(REPO / "scripts/run_agent.py", user, host, port, "run_agent.py")

    acfg = {"secret": secret, "claudePath": claude_path,
            "agent": {"host": "127.0.0.1", "port": 8766}, "control": {"host": "127.0.0.1", "port": 8765}}
    (RUN / "_dep.config.json").write_text(json.dumps(acfg, indent=2))
    scp(RUN / "_dep.config.json", user, host, port, "csm.config.json")
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

    # record machine in config (control re-reads per request; tunnels picks it up)
    if entry is None:
        entry = {"id": mid}
        machines.append(entry)
    entry.update({"name": entry.get("name", mid), "host": host, "sshUser": user, "sshPort": port,
                  "os": "windows", "agentPort": 8766, "localPort": localport,
                  "secret": secret, "pythonExe": pyexe, "claudePath": claude_path})
    CFG.write_text(json.dumps(cfg, indent=2))

    # nudge the tunnel manager to pick up the (new) machine
    plist = LA / "com.csm.tunnels.plist"
    if plist.exists():
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{__import__('os').getuid()}/com.csm.tunnels"], capture_output=True)
    time.sleep(3)
    print(f"✅ deployed '{mid}' ({user}@{host}) -> 127.0.0.1:{localport}")


if __name__ == "__main__":
    main()
