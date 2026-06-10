"""csm-agent — the per-machine workhorse.

A single-file stdlib HTTP service that owns this machine's ~/.claude: it lists
folders/sessions, launches detached `claude --remote-control` sessions via the
PTY supervisor, resolves the resulting claude.ai/code URL by polling pidfiles,
and deletes sessions. The central control plane (control.py) is the only
intended caller; auth is a shared bearer secret (constant-time compared).

Run:  python -m csm.agent  (reads csm.config.json next to the repo root)
"""

from __future__ import annotations

import hmac
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import __version__, codex, common, skills
from .config import load_config

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
LAUNCHER = HERE / ("launcher_win.py" if IS_WIN else "launcher.py")
# On Windows, run the launcher with pythonw (no console) + CREATE_NO_WINDOW so no
# terminal window flashes when a session is launched.
LAUNCH_EXE = sys.executable
if IS_WIN and sys.executable.lower().endswith("python.exe"):
    _pyw = sys.executable[:-len("python.exe")] + "pythonw.exe"
    if os.path.exists(_pyw):
        LAUNCH_EXE = _pyw

# token -> {cwd, name, started, state, url, error}
_launches: dict[str, dict] = {}
_lock = threading.Lock()


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.secret = cfg["secret"].encode()
        self.claude_path = cfg.get("claudePath") or _which_claude()
        self.log_dir = Path(cfg.get("logDir") or (common.claude_home() / ".csm-logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ---- handlers -------------------------------------------------------
    def health(self) -> dict:
        return {
            "ok": True,
            "hostname": socket.gethostname(),
            "os": _os_name(),
            "claudePath": self.claude_path,
            "claudeHome": str(common.claude_home()),
            "agentVersion": __version__,
        }

    def folders(self) -> dict:
        # merge Claude folders with Codex folders (same cwd -> one row)
        rows = common.list_folders()
        by_cwd = {f["cwd"]: f for f in rows}
        for cf in codex.list_folders():
            f = by_cwd.get(cf["cwd"])
            if f:
                f["session_count"] = f.get("session_count", 0) + cf["session_count"]
                f["codex_count"] = cf["session_count"]
                f["last_activity"] = max(f.get("last_activity") or 0, cf["last_activity"])
            else:
                cf["codex_count"] = cf["session_count"]
                rows.append(cf)
                by_cwd[cf["cwd"]] = cf
        return {"folders": rows}

    def sessions(self, cwd: str) -> dict:
        rows = common.list_sessions_for_cwd(cwd)
        for r in rows:
            r.setdefault("agent", "claude")
        rows += codex.list_sessions_for_cwd(cwd)
        rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
        return {"sessions": rows}

    def browse(self, path: str | None) -> dict:
        return common.browse_dir(path)

    def upload(self, body: dict) -> dict:
        """Save a base64 image to ~/.csm-uploads and return its path — used to
        attach images to codex runs (codex exec -i <path>)."""
        import base64
        name = os.path.basename(body.get("filename") or "image.png") or "image.png"
        try:
            data = base64.b64decode(body.get("data") or "")
        except Exception:
            data = b""
        if not data:
            return {"error": "no data"}, 400
        if len(data) > 20 * 1024 * 1024:
            return {"error": "image too large (>20MB)"}, 400
        d = Path.home() / ".csm-uploads"
        d.mkdir(exist_ok=True)
        p = d / f"{int(time.time() * 1000)}-{name}"
        p.write_bytes(data)
        return {"ok": True, "path": str(p)}

    def handoff(self, body: dict) -> dict:
        """Branch a session into a NEW session, optionally under the other agent.

        Both Claude and Codex store plain-text jsonl, so we serialize the source
        transcript (optionally truncated at `upToIndex` for a rewind) and seed a
        fresh target session with it.

        body: {sessionId, fromAgent, toAgent?, cwd, upToIndex?}
          - toAgent == fromAgent + upToIndex  -> rewind (same model, earlier point)
          - toAgent != fromAgent              -> model switch (handoff)
        Returns the codex {ok, sessionId, response} or the claude {token} to poll.
        """
        sid = body.get("sessionId") or ""
        src = body.get("fromAgent") or "claude"
        dst = body.get("toAgent") or src
        cwd = body.get("cwd") or ""
        up = body.get("upToIndex")
        if not sid:
            return {"ok": False, "error": "sessionId required"}
        tx = self.transcript(sid, src)
        if tx.get("error"):
            return {"ok": False, "error": f"source transcript: {tx['error']}"}
        msgs = tx.get("messages") or []
        if isinstance(up, int) and 0 <= up < len(msgs):
            msgs = msgs[:up + 1]          # keep through the chosen message
        if not msgs:
            return {"ok": False, "error": "nothing to carry over"}
        # keep the most recent turns within a budget so the seed stays sane
        budget, kept, used = 8000, [], 0
        for m in reversed(msgs):
            t = m.get("text") or ""
            if used + len(t) > budget and kept:
                break
            kept.append(m)
            used += len(t)
        kept.reverse()
        omitted = len(msgs) - len(kept)
        rewind = (dst == src)
        head = ("[Rewind] Resuming this conversation from an earlier point. "
                "The history below is the context up to that point — continue from here."
                if rewind else
                f"[Handoff] The conversation below was carried over from another coding "
                f"agent ({src}). Continue it as if it were your own context.")
        lines = [head]
        if omitted > 0:
            lines.append(f"(... {omitted} earlier messages omitted ...)")
        for m in kept:
            who = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"{who}: {m['text']}")
        lines += ["---", "Acknowledge briefly that you have the context, then continue."]
        seed = "\n".join(lines)
        if dst == "codex":
            r = codex.run({"cwd": cwd, "prompt": seed,
                           "timeoutSec": int(body.get("timeoutSec") or 240)})
            r["toAgent"] = "codex"
            return r
        tag = "rewind" if rewind else "handoff"
        r = self.launch({"cwd": cwd, "prompt": seed, "name": f"{tag}-{sid[:8]}"})
        if isinstance(r, tuple):
            return {"ok": False, "error": r[0].get("error", "launch failed")}
        r["ok"] = True
        r["toAgent"] = "claude"
        return r   # {token} — poll /launch?token= as with a normal launch

    def transcript(self, session_id: str, agent_kind: str = "") -> dict:
        if agent_kind == "codex":
            return codex.transcript(session_id)
        d = common.session_preview(session_id)
        if d.get("error") and not agent_kind:   # fall through for unlabeled codex ids
            c = codex.transcript(session_id)
            if not c.get("error"):
                return c
        return d

    def search(self, cwd: str, q: str) -> dict:
        return {"sessions": common.search_sessions(cwd, q)}

    def skills_list(self) -> dict:
        return {"skills": skills.list_skills(common.skills_dir())}

    def skills_pull(self, name: str | None = None) -> dict:
        return {"skills": skills.read_skills(common.skills_dir(), [name] if name else None)}

    def skills_apply(self, payload: dict) -> dict:
        return skills.write_skills(common.skills_dir(), payload)

    def launch(self, body: dict) -> dict:
        cwd = os.path.realpath(os.path.expanduser(body["cwd"]))
        if not os.path.isdir(cwd):
            return {"error": f"folder not found: {cwd}"}, 400
        user_name = (body.get("name") or "").strip()
        name = user_name or f"{socket.gethostname()}-{int(time.time())}"
        resume_id = body.get("resumeId") or ""
        fork = "1" if body.get("fork") else "0"
        token = secrets.token_hex(8)
        logf = open(self.log_dir / f"{token}.log", "wb")
        argv = [LAUNCH_EXE, str(LAUNCHER), cwd, name, resume_id, fork, self.claude_path]
        if (body.get("prompt") or "").strip():   # optional seed (rewind / model handoff)
            argv.append(body["prompt"])
        kw = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, close_fds=True)
        if IS_WIN:
            # detach so the session outlives the agent/SSH session
            kw["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kw["start_new_session"] = True
        subprocess.Popen(argv, **kw)
        with _lock:
            _launches[token] = {
                "cwd": cwd,
                "name": name,
                "user_name": user_name,
                "resume_id": resume_id,
                "started": time.time(),
                "state": "spawning",
                "url": None,
                "error": None,
            }
        return {"token": token}

    def launch_status(self, token: str) -> dict:
        with _lock:
            rec = _launches.get(token)
            rec = dict(rec) if rec else None
        if rec is None:
            return {"error": "unknown token"}, 404
        if rec["state"] in ("ready", "failed"):
            return rec
        # resolve: find a live pidfile for this cwd created after we started
        target = rec["cwd"]
        best = None
        for s in common.live_sessions():
            if not s.get("cwd") or not s.get("bridge_session_id"):
                continue
            if os.path.realpath(s["cwd"]) != target:
                continue
            started_s = (s.get("started_at") or 0) / 1000.0
            if started_s and started_s < rec["started"] - 5:
                continue  # an older pre-existing session, not ours
            if best is None or (s.get("started_at") or 0) > (best.get("started_at") or 0):
                best = s
        if best:
            url = common.bridge_url(best["bridge_session_id"])
            if rec.get("user_name"):  # remember the user's chosen name for the list
                common.set_session_name(best["session_id"], rec["user_name"])
            with _lock:
                if token in _launches:
                    _launches[token].update(state="ready", url=url)
            rec.update(state="ready", url=url)
        elif time.time() - rec["started"] > 45:
            err = "timed out waiting for session (check auth / trust prompt)"
            with _lock:
                if token in _launches:
                    _launches[token].update(state="failed", error=err)
            rec.update(state="failed", error=err)
        return rec

    def manager_open(self, cwd: str, fresh: bool = False) -> dict:
        """Open (or continue) the single persistent orchestration-manager session
        in `cwd`, returning its claude.ai/code URL. Reuses a live one, else
        resumes the most recent session there, else starts fresh. No prompt.

        If `fresh`, RESET the manager: stop any live session in `cwd` and start a
        brand-new one with blank context (no resume)."""
        cwd = os.path.realpath(os.path.expanduser(cwd))
        os.makedirs(cwd, exist_ok=True)
        if fresh:
            for s in common.live_sessions():
                if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd:
                    common.stop_session(s["session_id"])
            time.sleep(1.0)  # let the old PTY release before launching fresh
        else:
            for s in common.live_sessions():
                if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd and s.get("bridge_session_id"):
                    return {"ok": True, "sessionId": s["session_id"], "reused": True,
                            "bridgeUrl": common.bridge_url(s["bridge_session_id"])}
        past = [] if fresh else common.list_sessions_for_cwd(cwd)
        resume_id = past[0]["session_id"] if past else ""
        started = time.time()
        logf = open(self.log_dir / "manager.log", "wb")
        argv = [LAUNCH_EXE, str(LAUNCHER), cwd, "csm-manager", resume_id, "0", self.claude_path]
        kw = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, close_fds=True)
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(argv, **kw)
        while time.time() - started < 45:
            time.sleep(1.5)
            for s in common.live_sessions():
                if not s.get("cwd") or not s.get("bridge_session_id"):
                    continue
                if os.path.realpath(s["cwd"]) != cwd:
                    continue
                st = (s.get("started_at") or 0) / 1000.0
                if resume_id or not st or st >= started - 5:
                    return {"ok": True, "sessionId": s["session_id"],
                            "bridgeUrl": common.bridge_url(s["bridge_session_id"])}
        return {"ok": False, "error": "timed out opening manager"}

    def manager_delete(self, cwd: str) -> dict:
        """Delete the manager session: stop any live session in `cwd` and remove
        its stored sessions (blank slate; the next open starts brand new)."""
        cwd = os.path.realpath(os.path.expanduser(cwd))
        for s in common.live_sessions():
            if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd:
                common.stop_session(s["session_id"])
        time.sleep(0.5)  # let the PTY release before removing files
        return {"ok": True, "deleted": common.delete_folder_sessions(cwd)}

    def delete(self, session_id: str) -> dict:
        return common.delete_session(session_id)

    def stop(self, session_id: str) -> dict:
        return common.stop_session(session_id)

    def delete_folder(self, cwd: str) -> dict:
        return common.delete_folder_sessions(cwd)


def _make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter logs; sys.stderr is None under pythonw
            try:
                if sys.stderr:
                    sys.stderr.write("agent %s - %s\n" % (self.address_string(), fmt % args))
            except Exception:
                pass

        def _authed(self) -> bool:
            hdr = self.headers.get("Authorization", "")
            token = hdr[7:] if hdr.startswith("Bearer ") else ""
            return hmac.compare_digest(token.encode(), agent.secret)

        def _send(self, obj, status=200):
            if isinstance(obj, tuple):
                obj, status = obj
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/health":
                return self._send(agent.health())
            if u.path == "/folders":
                return self._send(agent.folders())
            if u.path == "/sessions":
                cwd = (q.get("cwd") or [""])[0]
                if not cwd:
                    return self._send({"error": "cwd required"}, 400)
                return self._send(agent.sessions(cwd))
            if u.path == "/launch":
                token = (q.get("token") or [""])[0]
                return self._send(agent.launch_status(token))
            if u.path == "/browse":
                return self._send(agent.browse((q.get("path") or [""])[0] or None))
            if u.path == "/transcript":
                sid = (q.get("sessionId") or [""])[0]
                if not sid:
                    return self._send({"error": "sessionId required"}, 400)
                return self._send(agent.transcript(sid, (q.get("agent") or [""])[0]))
            if u.path == "/search":
                cwd = (q.get("cwd") or [""])[0]
                if not cwd:
                    return self._send({"error": "cwd required"}, 400)
                return self._send(agent.search(cwd, (q.get("q") or [""])[0]))
            if u.path == "/skills":
                return self._send(agent.skills_list())
            if u.path == "/skills/pull":
                return self._send(agent.skills_pull((q.get("name") or [""])[0] or None))
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            u = urlparse(self.path)
            body = self._read_json()
            if u.path == "/launch":
                if not body or not body.get("cwd"):
                    return self._send({"error": "cwd required"}, 400)
                return self._send(agent.launch(body))
            if u.path == "/run":
                b = body or {}
                if b.get("agent") == "codex":
                    return self._send(codex.run(b))
                return self._send({"error": "only agent='codex' runs are supported"}, 400)
            if u.path == "/upload":
                return self._send(agent.upload(body or {}))
            if u.path == "/handoff":
                return self._send(agent.handoff(body or {}))
            if u.path == "/manager":
                b = body or {}
                cwd = b.get("cwd") or "~/.csm-manager"
                if b.get("delete"):
                    return self._send(agent.manager_delete(cwd))
                return self._send(agent.manager_open(cwd, fresh=bool(b.get("fresh"))))
            if u.path == "/stop":
                if not body or not body.get("sessionId"):
                    return self._send({"error": "sessionId required"}, 400)
                return self._send(agent.stop(body["sessionId"]))
            if u.path == "/folder/delete":
                if not body or not body.get("cwd"):
                    return self._send({"error": "cwd required"}, 400)
                return self._send(agent.delete_folder(body["cwd"]))
            if u.path == "/skills/apply":
                return self._send(agent.skills_apply((body or {}).get("skills") or {}))
            return self._send({"error": "not found"}, 404)

        def do_DELETE(self):
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/session":
                sid = (q.get("sessionId") or [""])[0]
                if not sid:
                    return self._send({"error": "sessionId required"}, 400)
                if (q.get("agent") or [""])[0] == "codex":
                    return self._send(codex.delete(sid))
                return self._send(agent.delete(sid))
            return self._send({"error": "not found"}, 404)

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return None
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return None

    return Handler


def _which_claude() -> str:
    import shutil

    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def _os_name() -> str:
    s = platform.system().lower()
    return {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(s, s)


def main():
    # Under pythonw.exe (no console) sys.stdout/stderr are None; any write crashes
    # the process. Redirect them to a log file so the agent runs headless cleanly.
    if sys.stdout is None or sys.stderr is None:
        logf = open(common.claude_home() / "csm-agent.log", "a", buffering=1, encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = logf
        if sys.stderr is None:
            sys.stderr = logf
    cfg = load_config()
    agent = Agent(cfg)
    host = cfg["agent"]["host"]
    port = int(cfg["agent"]["port"])
    # Keep default allow_reuse_address=True so the agent can rebind after a
    # restart (Windows would otherwise block on a TIME_WAIT socket). Duplicate
    # instances are prevented by running a single scheduled task, not here.
    httpd = ThreadingHTTPServer((host, port), _make_handler(agent))
    sys.stderr.write(f"csm-agent {__version__} on http://{host}:{port} (claude={agent.claude_path})\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
