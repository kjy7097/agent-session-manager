"""Named Claude logins for one machine, switchable without touching a console.

Claude Code keeps one login per config directory, so an account here is just a
directory handed to claude as CLAUDE_CONFIG_DIR. What it must NOT get its own
copy of is the session history: `projects` and `sessions` live under the config
dir, so a second account would write its transcripts somewhere the rest of this
program never looks. Those, plus settings and skills, are linked back to the
real ~/.claude; only the login differs between accounts.

`.claude.json` is deliberately not shared — it holds `oauthAccount`, so sharing
it would defeat the separation. Its folder-trust record is per account as a
result, which costs one auto-answered trust prompt the first time an account
opens a folder.

Login runs `claude auth login` over plain pipes (no PTY, so Windows needs
nothing extra): it prints an OAuth URL and then waits on stdin for the code the
browser hands back, which is exactly the two-step a web UI can drive.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import common

IS_WIN = os.name == "nt"

# Entries an account borrows from the real ~/.claude instead of owning. CLAUDE.md
# is the user's own instructions, not something that belongs to a login, and
# without it a second account would quietly run without them.
SHARED = ("projects", "sessions", "settings.json", "skills", "CLAUDE.md")

_logins: dict[str, dict] = {}     # token -> {proc, name, url, started, output}
_lock = threading.Lock()
LOGIN_TTL = 15 * 60


def accounts_dir() -> Path:
    return Path.home() / ".claude-accounts"


def _active_file() -> Path:
    return common.claude_home() / ".csm-active-account"


def _valid(name: str) -> bool:
    return bool(name) and name == os.path.basename(name) and name not in (".", "..") \
        and not any(c in name for c in '\\/:*?"<>|')


def _env_for(name: str) -> dict:
    """claude resolves its config file relative to CLAUDE_CONFIG_DIR, and the
    default install keeps that file at ~/.claude.json — *outside* ~/.claude. So
    pointing the variable at ~/.claude is not a no-op: it sends claude looking in
    the wrong place and the machine's own login reads as logged out. The default
    account is therefore the one with the variable removed, not set."""
    env = dict(os.environ)
    if name:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir_for(name))
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def config_dir_for(name: str) -> Path:
    """Where claude should look for this account. "" is the machine's original
    ~/.claude, which stays the default so an untouched machine behaves as before."""
    if not name:
        return common.claude_home()
    return accounts_dir() / name


def active() -> str:
    try:
        return _active_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_active(name: str) -> dict:
    if name and not (accounts_dir() / name).is_dir():
        return {"error": f"unknown account: {name}"}
    seed_config(name)      # also repairs accounts made before this existed
    try:
        if name:
            _active_file().write_text(name, encoding="utf-8")
        elif _active_file().exists():
            _active_file().unlink()
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "active": active()}


def status(name: str, timeout: int = 20) -> dict:
    """`claude auth status --json` for one account."""
    env = _env_for(name)
    exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"loggedIn": False, "error": str(e)}
    out = (r.stdout or "").strip()
    i = out.find("{")
    if i < 0:
        return {"loggedIn": False, "error": (r.stderr or out or "no output")[:200]}
    try:
        return json.loads(out[i:])
    except json.JSONDecodeError as e:
        return {"loggedIn": False, "error": str(e)}


def _row(name: str) -> dict:
    st = status(name)
    return {
        "name": name,
        "label": name or "기본",
        "isDefault": not name,
        "active": name == active(),
        "loggedIn": bool(st.get("loggedIn")),
        "email": st.get("email"),
        "orgName": st.get("orgName"),
        "subscriptionType": st.get("subscriptionType"),
        "authMethod": st.get("authMethod"),
        "error": st.get("error"),
    }


def list_accounts() -> dict:
    """The machine's own login first, then every named account. Each is queried
    in its own thread: claude takes a beat to answer and they don't depend on
    each other."""
    names = [""]
    d = accounts_dir()
    if d.is_dir():
        names += sorted(p.name for p in d.iterdir() if p.is_dir())
    rows: list = [None] * len(names)

    def fill(i, n):
        try:
            rows[i] = _row(n)
        except Exception as e:      # never let one bad account hide the rest
            rows[i] = {"name": n, "label": n or "기본", "isDefault": not n,
                       "active": n == active(), "loggedIn": False, "error": str(e)}

    ts = [threading.Thread(target=fill, args=(i, n), daemon=True) for i, n in enumerate(names)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=25)
    return {"accounts": [r for r in rows if r], "active": active()}


def _link(src: Path, dst: Path) -> None:
    """Point dst at src. Junctions and symlinks both need the target to exist,
    and on Windows a file symlink needs privileges a directory junction doesn't,
    so fall back to copying a plain file."""
    if dst.exists() or dst.is_symlink():
        return
    if not src.exists():
        if src.name.endswith(".json"):
            return
        src.mkdir(parents=True, exist_ok=True)
    try:
        if IS_WIN and src.is_dir():
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           capture_output=True, check=True)
        else:
            dst.symlink_to(src, target_is_directory=src.is_dir())
    except (OSError, subprocess.SubprocessError):
        if src.is_file():
            shutil.copy2(src, dst)


# Carried from the machine's own config into a new account. Deliberately a short
# allowlist, not "everything but the login": the rest of that file is caches keyed
# to an org and a model roster, and handing one account another's would be wrong.
INHERIT = ("hasCompletedOnboarding", "lastOnboardingVersion",
           "tipsHistory", "tipsHistoryByCommand", "projects")


def _config_file(name: str) -> Path:
    """claude keeps this file beside the config dir for the default account and
    inside it for a named one."""
    return (Path.home() / ".claude.json") if not name else (config_dir_for(name) / ".claude.json")


def seed_config(name: str) -> None:
    """Make a fresh account usable without a console.

    A new config dir has never been through first run, so claude opens its
    onboarding wizard — a theme picker the launcher has no answer for — and the
    launch just times out. It also has no record of which folders are trusted.
    Both live in .claude.json, which accounts can't share because it carries
    oauthAccount, so copy across the few keys that are about this machine rather
    than about who is signed in. Existing values win: this must never undo
    something the account already decided.
    """
    if not name:
        return
    src, dst = _config_file(""), _config_file(name)
    try:
        base = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        base = {}
    try:
        cur = json.loads(dst.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cur = {}
    changed = False
    for k in INHERIT:
        if k in base and k not in cur:
            cur[k] = base[k]
            changed = True
    if changed:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        except OSError:
            pass


def create(name: str) -> dict:
    if not _valid(name):
        return {"error": "invalid account name"}
    d = accounts_dir() / name
    if d.is_dir():
        return {"error": f"account already exists: {name}"}
    try:
        d.mkdir(parents=True)
    except OSError as e:
        return {"error": str(e)}
    home = common.claude_home()
    for entry in SHARED:
        _link(home / entry, d / entry)
    seed_config(name)
    return {"ok": True, "name": name, "configDir": str(d)}


def delete(name: str) -> dict:
    """Removes the account's own files. The shared entries are links, so the
    real history is never what gets deleted here."""
    if not _valid(name):
        return {"error": "invalid account name"}
    d = accounts_dir() / name
    if not d.is_dir():
        return {"error": f"unknown account: {name}"}
    for entry in SHARED:                       # unlink before rmtree, never follow
        p = d / entry
        try:
            if p.is_symlink() or (IS_WIN and p.is_dir()):
                p.unlink() if p.is_symlink() else os.rmdir(p)
        except OSError:
            pass
    try:
        shutil.rmtree(d)
    except OSError as e:
        return {"error": str(e)}
    if active() == name:
        set_active("")
    return {"ok": True, "deleted": name}


# ---------------------------------------------------------------- login flow

def _sweep() -> None:
    now = time.time()
    for tok, rec in list(_logins.items()):
        if now - rec["started"] > LOGIN_TTL:
            try:
                rec["proc"].kill()
            except Exception:
                pass
            _logins.pop(tok, None)


def login_start(name: str, console: bool = False) -> dict:
    """Begin a login and return the URL to open. The process stays alive waiting
    for the code, so the caller must come back with login_code()."""
    _sweep()
    d = config_dir_for(name)
    if name and not d.is_dir():
        return {"error": f"unknown account: {name}"}
    exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    argv = [exe, "auth", "login"] + (["--console"] if console else [])
    env = _env_for(name)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, bufsize=0)
    except OSError as e:
        return {"error": str(e)}

    rec = {"proc": proc, "name": name, "url": None, "started": time.time(), "output": b""}

    def pump():
        while True:
            try:
                b = proc.stdout.read(1)
            except (OSError, ValueError):
                break
            if not b:
                break
            rec["output"] += b
            if rec["url"] is None:
                i = rec["output"].find(b"https://")
                if i >= 0:
                    tail = rec["output"][i:]
                    j = min([x for x in (tail.find(b"\n"), tail.find(b" ")) if x >= 0] or [-1])
                    if j >= 0:
                        rec["url"] = tail[:j].decode("utf-8", "replace").strip()

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + 25
    while time.time() < deadline and rec["url"] is None and proc.poll() is None:
        time.sleep(0.2)
    if rec["url"] is None:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": "login did not offer a URL",
                "output": rec["output"].decode("utf-8", "replace")[-400:]}

    token = secrets.token_hex(8)
    with _lock:
        _logins[token] = rec
    return {"token": token, "url": rec["url"], "name": name}


def login_code(token: str, code: str) -> dict:
    """Hand the browser's code to a waiting login and report what claude made of it."""
    with _lock:
        rec = _logins.get(token)
    if rec is None:
        return {"error": "unknown or expired login"}
    proc = rec["proc"]
    try:
        proc.stdin.write((code.strip() + "\n").encode())
        proc.stdin.flush()
    except (OSError, ValueError) as e:
        return {"error": f"login is no longer running: {e}"}
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        return {"error": "timed out completing login"}
    with _lock:
        _logins.pop(token, None)
    seed_config(rec["name"])
    st = status(rec["name"])
    if not st.get("loggedIn"):
        return {"error": "login did not complete",
                "output": rec["output"].decode("utf-8", "replace")[-400:]}
    return {"ok": True, "account": _row(rec["name"])}


def logout(name: str) -> dict:
    exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    env = _env_for(name)
    try:
        subprocess.run([exe, "auth", "logout"], capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    return {"ok": True, "account": _row(name)}
