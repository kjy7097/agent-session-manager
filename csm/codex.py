"""Codex CLI adapter — read sessions from ~/.codex/sessions and run codex exec.

Codex stores each session as a rollout JSONL:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl
with `session_meta` (id, cwd, timestamp) and `response_item` (role + content
blocks) records. This module mirrors csm.common's Claude-session reads so the
agent can serve both behind one API; rows carry agent="codex".

Running/resuming uses `codex exec --json [resume <id>] <prompt>` (non-
interactive); the model backend is whatever the machine's codex is set up with
(ChatGPT login or a local provider).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def sessions_dir() -> Path:
    return codex_home() / "sessions"


def codex_path() -> str | None:
    """Locate the codex binary (None when not installed)."""
    p = shutil.which("codex")
    if p:
        return p
    cand = Path.home() / ".local" / "bin" / ("codex.exe" if IS_WIN else "codex")
    return str(cand) if cand.exists() else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b.get("text") or b.get("input_text") or b.get("output_text") or "")
        return "".join(out)
    return ""


def parse_rollout(path: Path, want_msgs: bool = False) -> dict | None:
    """One rollout jsonl -> session row (mirrors common.parse_session keys)."""
    sid = cwd = None
    first_prompt = None
    model = None
    msgs: list = []
    last_ts = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = o.get("type")
                p = o.get("payload") or {}
                ts = o.get("timestamp") or ""
                if ts:
                    last_ts = max(last_ts, ts)
                if t == "session_meta":
                    sid = p.get("id") or sid
                    cwd = p.get("cwd") or cwd
                elif t == "turn_context" and p.get("model"):
                    model = p["model"]
                elif t == "response_item" and p.get("role") in ("user", "assistant"):
                    role = p["role"]
                    txt = _text(p.get("content")).strip()
                    if not txt or txt.startswith("<"):
                        continue  # developer scaffolding / environment_context
                    if want_msgs:
                        msgs.append({"role": role, "text": txt[:12000], "ts": ts})
                    else:
                        msgs.append(1)
                    if role == "user" and first_prompt is None:
                        first_prompt = txt[:200]
    except OSError:
        return None
    if not sid:
        sid = path.stem.split("-", 1)[-1][-36:]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    from .common import _epoch  # ISO -> epoch (shared helper)
    row = {
        "session_id": sid,
        "agent": "codex",
        "cwd": cwd,
        "title": first_prompt or "(no title)",
        "first_prompt": first_prompt or "",
        "last_activity": _epoch(last_ts) or mtime,
        "msg_count": len(msgs),
        "model": model or "",
        "git_branch": "",
        "is_remote_control": False,
        "bridge_session_id": None,
        "is_live": False,
        "bridge_url": None,
        "rollout_path": str(path),
    }
    if want_msgs:
        row["messages"] = msgs
    return row


def _all_rollouts():
    d = sessions_dir()
    if not d.is_dir():
        return
    yield from d.rglob("rollout-*.jsonl")


def list_sessions_for_cwd(cwd: str) -> list[dict]:
    cwd = os.path.realpath(os.path.expanduser(cwd))
    rows = []
    for f in _all_rollouts():
        r = parse_rollout(f)
        if r and r.get("cwd") and os.path.realpath(r["cwd"]) == cwd:
            rows.append(r)
    rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
    return rows


def list_folders() -> list[dict]:
    """cwd -> {cwd, session_count, last_activity} for codex sessions."""
    by_cwd: dict[str, dict] = {}
    for f in _all_rollouts():
        r = parse_rollout(f)
        if not r or not r.get("cwd"):
            continue
        g = by_cwd.setdefault(r["cwd"], {"cwd": r["cwd"], "session_count": 0, "last_activity": 0})
        g["session_count"] += 1
        g["last_activity"] = max(g["last_activity"], r.get("last_activity") or 0)
    return list(by_cwd.values())


def transcript(session_id: str) -> dict:
    for f in _all_rollouts():
        if session_id not in f.name:
            continue
        r = parse_rollout(f, want_msgs=True)
        if r and r["session_id"] == session_id:
            return {"session_id": session_id, "agent": "codex", "title": r["title"],
                    "total": len(r["messages"]), "messages": r["messages"]}
    # slow path (meta id may differ from filename)
    for f in _all_rollouts():
        r = parse_rollout(f, want_msgs=True)
        if r and r["session_id"] == session_id:
            return {"session_id": session_id, "agent": "codex", "title": r["title"],
                    "total": len(r["messages"]), "messages": r["messages"]}
    return {"error": "not found"}


def delete(session_id: str) -> dict:
    """Delete a codex session by removing its rollout jsonl."""
    for f in _all_rollouts():
        if session_id in f.name:
            try:
                f.unlink()
                return {"ok": True, "agent": "codex"}
            except OSError as e:
                return {"ok": False, "reason": str(e)}
    # slow path: id only inside the file's session_meta
    for f in _all_rollouts():
        r = parse_rollout(f)
        if r and r["session_id"] == session_id:
            try:
                f.unlink()
                return {"ok": True, "agent": "codex"}
            except OSError as e:
                return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": "not found"}


def run(body: dict) -> dict:
    """Start or resume a codex session non-interactively and return the reply.

    body: {cwd, prompt, resumeId?, images?: [paths], timeoutSec?}
    Returns {ok, sessionId, response} (response = last agent message).
    """
    cx = codex_path()
    if not cx:
        return {"ok": False, "error": "codex CLI not installed on this machine"}
    cwd = os.path.realpath(os.path.expanduser(body.get("cwd", "") or "~"))
    prompt = (body.get("prompt") or "").strip()
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"folder not found: {cwd}"}
    if not prompt:
        return {"ok": False, "error": "prompt required"}
    resume_id = body.get("resumeId") or ""
    timeout = int(body.get("timeoutSec") or 240)

    argv = [cx, "exec", "--json", "--skip-git-repo-check", "-s", "read-only"]
    if (body.get("model") or "").strip():
        argv += ["-m", body["model"].strip()]
    if resume_id:
        argv += ["resume", resume_id]   # -i after `resume` attaches to the resumed turn
    for img in body.get("images") or []:
        argv += ["-i", img]
    if body.get("images"):
        argv.append("--")  # stop -i <FILE>... from swallowing the prompt
    argv.append(prompt)
    try:
        r = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "codex timed out", "sessionId": resume_id or None}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    out = r.stdout.decode("utf-8", "replace")
    sid = resume_id or None
    answer = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not sid and ev.get("type") == "thread.started" and ev.get("thread_id"):
            sid = ev["thread_id"]
        if ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                answer = item["text"]
    if not sid:
        err = r.stderr.decode("utf-8", "replace")[-400:]
        return {"ok": False, "error": "codex gave no session id", "raw": (out[-400:] + err)}
    return {"ok": True, "sessionId": sid, "agent": "codex", "response": answer or "(no reply)"}
