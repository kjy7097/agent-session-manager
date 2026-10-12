"""Keep every machine current without anyone deploying by hand.

Two independent jobs:

* Claude Code, on each machine (run by the agent). claude's own updater only
  runs while someone uses claude there, so a machine that was off — or only
  ever runs sessions launched from here — falls behind. Once the network is up
  after the agent starts (that is, after a boot), run `claude update`, then
  again every few hours.

* The agent's own code, on each remote machine (run by the control plane).
  Every agent reports a hash of each csm/*.py it started with; when one that
  was offline comes back with files that differ from the committed code here,
  copy the files over SSH (the same key the tunnels use), restart the agent,
  and put the old files back if the new ones do not come up.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
REPO = PKG.parent


def code_hashes(folder: Path = PKG) -> dict:
    """sha256 of each module in the package, as found on disk."""
    out = {}
    for f in sorted(folder.glob("*.py")):
        try:
            out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


# what this process started with — a restart is what makes new files count
LOADED = code_hashes()


# ---- Claude Code (agent side) ---------------------------------------------

_claude = {"state": "waiting", "version": "", "lastRun": 0.0, "lastOk": 0.0, "message": ""}
_claude_lock = threading.Lock()

CLAUDE_EVERY = 6 * 3600       # after a good run
CLAUDE_RETRY = (900, 3600, 3 * 3600)   # after failed runs while online


def _online(timeout: float = 5) -> bool:
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout).close()
        return True
    except OSError:
        return False


def _run_claude(claude_path: str, *args, timeout=600):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    return subprocess.run([claude_path, *args], capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env, **kw)


def _last_line(b: bytes) -> str:
    lines = [ln.strip() for ln in b.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def claude_status() -> dict:
    with _claude_lock:
        return dict(_claude)


def _claude_loop(claude_path: str):
    fails = 0
    time.sleep(15)            # let the agent come up first
    while True:
        while not _online():
            with _claude_lock:
                _claude["state"] = "offline"
            time.sleep(30)
        with _claude_lock:
            _claude.update(state="updating", lastRun=time.time())
        try:
            r = _run_claude(claude_path, "update")
            ok = r.returncode == 0
            msg = _last_line(r.stdout + b"\n" + r.stderr)
        except (OSError, subprocess.SubprocessError) as e:
            ok, msg = False, str(e)[:300]
        version = ""
        try:
            version = _last_line(_run_claude(claude_path, "--version", timeout=30).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        with _claude_lock:
            _claude.update(state="ok" if ok else "failed", message=msg, version=version)
            if ok:
                _claude["lastOk"] = time.time()
        sys.stderr.write(f"[update] claude update: {'ok' if ok else 'failed'} — {msg}\n")
        if ok:
            fails = 0
            time.sleep(CLAUDE_EVERY)
        else:
            time.sleep(CLAUDE_RETRY[min(fails, len(CLAUDE_RETRY) - 1)])
            fails += 1


def start_claude_updates(cfg: dict, claude_path: str):
    if cfg.get("autoUpdateClaude", True) is False:
        with _claude_lock:
            _claude["state"] = "disabled"
        return
    threading.Thread(target=_claude_loop, args=(claude_path,), daemon=True,
                     name="claude-update").start()


# ---- agent code (control side) ---------------------------------------------

CHECK_EVERY = 30
COME_BACK = 120               # how long a restarted agent gets to answer again

# machine id -> {state, at, message, pushed}
status: dict = {}
_failed: dict = {}            # machine id -> (release, not before)
RETRY_AFTER = 600             # a push that failed on the way, not in the code


_release: dict = {"head": None, "files": {}}


def release() -> dict:
    """{name: bytes} of the committed csm/*.py — never a half-edited working
    tree. Falls back to the files on disk outside a git checkout."""
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        if head == _release["head"]:
            return _release["files"]
        ls = subprocess.run(["git", "-C", str(REPO), "ls-tree", "HEAD", "csm/"],
                            capture_output=True, text=True, timeout=20, check=True).stdout
        out = {}
        for line in ls.splitlines():
            meta, path = line.split("\t", 1)
            name = path.split("/", 1)[1]
            if meta.split()[1] == "blob" and name.endswith(".py"):
                out[name] = subprocess.run(["git", "-C", str(REPO), "cat-file", "blob", meta.split()[2]],
                                           capture_output=True, timeout=20, check=True).stdout
        if out:
            _release.update(head=head, files=out)
            return out
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return {f.name: f.read_bytes() for f in sorted(PKG.glob("*.py"))}


def _hashes(files: dict) -> dict:
    return {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}


def _stale(loaded: dict, want: dict) -> list:
    return sorted(n for n, h in want.items() if loaded.get(n) != h)


_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=12"]


def _ssh(m: dict, cmd: str, timeout=120):
    target = f"{m['sshUser']}@{m['host']}" if m.get("sshUser") else m["host"]
    return subprocess.run(["ssh", *_SSH_OPTS, "-p", str(m.get("sshPort", 22)), target, cmd],
                          capture_output=True, timeout=timeout)


def _scp(m: dict, files: list, dest: str):
    target = f"{m['sshUser']}@{m['host']}" if m.get("sshUser") else m["host"]
    return subprocess.run(["scp", "-q", *_SSH_OPTS, "-P", str(m.get("sshPort", 22)),
                           *[str(f) for f in files], f"{target}:{dest}"],
                          capture_output=True, timeout=300)


def _ps(script: str) -> str:
    """A PowerShell script as one ssh command, with no quoting to get wrong."""
    script = "$ProgressPreference='SilentlyContinue'; " + script
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + \
        base64.b64encode(script.encode("utf-16-le")).decode()


# The package is ~/csm itself (the task runs `python -m csm.agent` from ~);
# the previous files wait in ~/csm-prev, outside the package.
_WIN_BACKUP = _ps(
    "$ErrorActionPreference='Stop'; New-Item -ItemType Directory -Force csm-prev | Out-Null;"
    "Remove-Item csm-prev\\*.py -EA SilentlyContinue; Copy-Item csm\\*.py csm-prev\\")
_WIN_ROLLBACK = _ps("Copy-Item csm-prev\\*.py csm\\ -Force")
# only the agent: every live session's launcher is a python process too
_WIN_RESTART = _ps(
    "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
    "Where-Object { $_.CommandLine -like '*csm.agent*' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
    "Start-Sleep 1; schtasks /run /tn CSM-Agent | Out-Null")
_NIX_BACKUP = "mkdir -p csm-prev && rm -f csm-prev/*.py && cp csm/*.py csm-prev/"
_NIX_ROLLBACK = "cp -f csm-prev/*.py csm/"
_NIX_RESTART = ("export XDG_RUNTIME_DIR=/run/user/$(id -u); "
                "systemctl --user restart csm-agent")


def _restart(m: dict):
    return _ssh(m, _WIN_RESTART if m.get("os") == "windows" else _NIX_RESTART)


def _push(m: dict, files: dict, names: list, health, want: dict) -> str:
    """Copy `names` over, restart, and wait for the agent to report them.
    Returns '' on success, else what went wrong (after rolling back)."""
    win = m.get("os") == "windows"
    stage = Path.home() / ".csm-run" / "update" / m["id"]
    stage.mkdir(parents=True, exist_ok=True)
    for old in stage.glob("*.py"):
        old.unlink()
    for n in names:
        (stage / n).write_bytes(files[n])
    r = _ssh(m, _WIN_BACKUP if win else _NIX_BACKUP)
    if r.returncode != 0:
        return "backup failed: " + _last_line(r.stderr or r.stdout)
    r = _scp(m, [stage / n for n in names], "csm/")
    if r.returncode != 0:
        _ssh(m, _WIN_ROLLBACK if win else _NIX_ROLLBACK)
        return "copy failed: " + _last_line(r.stderr)
    _restart(m)
    deadline = time.time() + COME_BACK
    while time.time() < deadline:
        time.sleep(5)
        h = health(m["id"])
        if h and not _stale(h.get("codeFiles") or {}, want):
            return ""
    _ssh(m, _WIN_ROLLBACK if win else _NIX_ROLLBACK)
    _restart(m)
    return "BROKEN: the new agent did not come up; previous files restored"


def _check(m: dict, health, busy) -> None:
    mid = m["id"]
    h = health(mid)
    if h is None:
        status[mid] = {"state": "offline", "at": time.time()}
        return
    files = release()
    want = _hashes(files)
    stale = _stale(h.get("codeFiles") or {}, want)
    if not stale:
        status[mid] = {"state": "current", "at": time.time()}
        return
    tag = hashlib.sha256(repr(sorted(want.items())).encode()).hexdigest()[:12]
    ftag, until = _failed.get(mid, ("", 0))
    if ftag == tag and time.time() < until:
        return                 # a release that broke it waits for the next commit
    if h.get("busy") or busy(mid):
        status[mid] = {"state": "waiting", "at": time.time(), "message": "busy", "files": stale}
        return
    status[mid] = {"state": "updating", "at": time.time(), "files": stale}
    sys.stderr.write(f"[update] {mid}: pushing {', '.join(stale)}\n")
    try:
        err = _push(m, files, stale, health, want)
    except (OSError, subprocess.SubprocessError) as e:
        err = str(e)
    if err:
        # code that does not start is not retried; a dropped connection is
        _failed[mid] = (tag, float("inf") if err.startswith("BROKEN") else time.time() + RETRY_AFTER)
        status[mid] = {"state": "failed", "at": time.time(), "message": err, "release": tag}
        sys.stderr.write(f"[update] {mid}: {err}\n")
    else:
        status[mid] = {"state": "current", "at": time.time(), "updated": stale, "release": tag}
        sys.stderr.write(f"[update] {mid}: updated to {tag}\n")


def start_code_sync(get_machines, health, busy):
    """get_machines() -> remote machine entries from the config;
    health(mid) -> the agent's /health dict or None; busy(mid) -> bool."""

    def loop():
        time.sleep(10)
        while True:
            for m in get_machines():
                if m.get("autoUpdate") is False or not m.get("host") or m.get("os") == "macos":
                    continue
                try:
                    _check(m, health, busy)
                except Exception as e:      # never let one machine stop the loop
                    sys.stderr.write(f"[update] {m.get('id')}: {e}\n")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, daemon=True, name="code-sync").start()
