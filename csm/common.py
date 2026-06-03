"""Filesystem-level reads of Claude Code's local session store.

Everything here is verified against claude 2.1.161 on macOS:

- Sessions live at ~/.claude/projects/<ENCODED_CWD>/<session-uuid>.jsonl
- ENCODED_CWD is LOSSY (every non [A-Za-z0-9-] char -> '-'), so we NEVER reverse
  the dir name; the authoritative cwd is read from the jsonl content.
- Live sessions are described by ~/.claude/sessions/<pid>.json (pid is the
  re-exec'd grandchild, so match by cwd + recency + liveness, never by pid).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def projects_dir() -> Path:
    return claude_home() / "projects"


def sessions_dir() -> Path:
    return claude_home() / "sessions"


def skills_dir() -> Path:
    return claude_home() / "skills"


def _names_file() -> Path:
    return claude_home() / ".csm-session-names.json"


def session_names() -> dict:
    """User-chosen session names {sessionId: name}, recorded by the agent at launch
    (the remote-control name isn't stored in the pidfile/jsonl)."""
    try:
        return json.loads(_names_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def set_session_name(session_id: str, name: str) -> None:
    if not session_id or not name:
        return
    d = session_names()
    d[session_id] = name
    try:
        _names_file().write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _text(content) -> str:
    """Normalize a message 'content' field (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _iso(ms_or_str):
    """Best-effort millis-epoch -> iso string passthrough."""
    if isinstance(ms_or_str, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms_or_str / 1000))
    return ms_or_str


def parse_session(path: Path) -> dict:
    """Single-pass parse of one session jsonl into a picker row.

    Returns keys: session_id, cwd, title, first_prompt, last_activity (epoch
    seconds), msg_count, git_branch, is_remote_control, bridge_session_id.
    """
    session_id = path.stem
    cwd = None
    ai_title = None
    first_prompt = None
    git_branch = None
    bridge_session_id = None
    is_remote_control = False
    msg_count = 0
    last_ts = 0.0

    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = obj.get("type")
                if cwd is None and obj.get("cwd"):
                    cwd = obj["cwd"]
                if git_branch is None and obj.get("gitBranch"):
                    git_branch = obj["gitBranch"]

                ts = obj.get("timestamp")
                if isinstance(ts, str):
                    # cheap lexical compare via parsed epoch fallback to mtime later
                    last_ts = max(last_ts, _epoch(ts))

                if t == "ai-title":
                    ai_title = obj.get("aiTitle") or ai_title
                elif t == "user":
                    msg_count += 1
                    if first_prompt is None and not obj.get("isMeta"):
                        msg = obj.get("message") or {}
                        txt = _text(msg.get("content")).strip()
                        if txt and not txt.startswith("<"):
                            first_prompt = txt[:200]
                elif t == "assistant":
                    msg_count += 1
                elif t == "bridge-session":
                    is_remote_control = True
                    bridge_session_id = obj.get("bridgeSessionId") or bridge_session_id
                elif t == "system" and obj.get("subtype") == "bridge_status":
                    is_remote_control = True
    except OSError:
        pass

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    last_activity = last_ts or mtime

    title = ai_title or first_prompt or "(no title)"
    return {
        "session_id": session_id,
        "cwd": cwd,
        "title": title,
        "first_prompt": first_prompt or "",
        "last_activity": last_activity,
        "msg_count": msg_count,
        "git_branch": git_branch or "",
        "is_remote_control": is_remote_control,
        "bridge_session_id": bridge_session_id,
    }


def session_preview(session_id: str, limit: int = 40) -> dict:
    """Readable transcript preview (last `limit` real user/assistant messages)
    so the UI can show what a session was about before resuming it."""
    f = find_session_file(session_id)
    if f is None:
        return {"error": "not found"}
    title = None
    msgs = []
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "ai-title":
                    title = obj.get("aiTitle") or title
                elif t in ("user", "assistant"):
                    if obj.get("isMeta"):
                        continue
                    txt = _text((obj.get("message") or {}).get("content")).strip()
                    if not txt or txt.startswith("<"):
                        continue
                    msgs.append({"role": t, "text": txt[:1500], "ts": obj.get("timestamp")})
    except OSError as e:
        return {"error": str(e)}
    return {
        "session_id": session_id,
        "title": title or "(no title)",
        "total": len(msgs),
        "messages": msgs[-limit:],
    }


def _epoch(iso: str) -> float:
    """Parse an ISO-8601 timestamp to epoch seconds; 0 on failure."""
    try:
        from datetime import datetime

        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _first_cwd(path: Path) -> str | None:
    """Read just enough lines to learn the session's launch cwd (fast)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        pass
    return None


def list_folders() -> list[dict]:
    """Group every session by its real launch cwd.

    Returns [{cwd, session_count, last_activity}] sorted by recency.
    Dirs whose sessions have no recorded cwd are grouped under their dir name.
    Live-but-unwritten sessions (just opened, no jsonl yet) are folded in so a
    freshly launched folder shows up immediately.
    """
    groups: dict[str, dict] = {}
    seen_ids: dict[str, set] = {}
    pdir = projects_dir()
    if pdir.is_dir():
        for d in pdir.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                cwd = _first_cwd(f) or f"(unknown:{d.name})"
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = 0.0
                g = groups.setdefault(cwd, {"cwd": cwd, "session_count": 0, "last_activity": 0.0, "live": 0})
                g["session_count"] += 1
                g["last_activity"] = max(g["last_activity"], mtime)
                seen_ids.setdefault(cwd, set()).add(f.stem)
    for s in live_sessions():
        if not s.get("cwd"):
            continue
        cwd = s["cwd"]
        g = groups.setdefault(cwd, {"cwd": cwd, "session_count": 0, "last_activity": 0.0, "live": 0})
        g["live"] += 1
        if s.get("session_id") not in seen_ids.get(cwd, ()):  # not yet counted via jsonl
            g["session_count"] += 1
        g["last_activity"] = max(g["last_activity"], (s.get("updated_at") or 0) / 1000.0)
    return sorted(groups.values(), key=lambda g: g["last_activity"], reverse=True)


def list_sessions_for_cwd(cwd: str) -> list[dict]:
    """All sessions whose launch cwd == the given path (real path match)."""
    target = _realpath(cwd)
    # "(unknown:<dir>)" pseudo-folder groups cwd-less stub sessions by their
    # physical project dir so they remain listable/deletable.
    unknown_dir = cwd[len("(unknown:"):-1] if cwd.startswith("(unknown:") and cwd.endswith(")") else None
    live = {s["session_id"]: s for s in live_sessions() if s.get("session_id")}
    names = session_names()  # user-chosen names {sessionId: name}
    rows = []
    seen = set()
    pdir = projects_dir()
    if pdir.is_dir():
        for d in pdir.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                fc = _first_cwd(f)
                if unknown_dir is not None:
                    if not (fc is None and d.name == unknown_dir):
                        continue
                elif fc is None or _realpath(fc) != target:
                    continue
                row = parse_session(f)
                ls = live.get(row["session_id"])
                row["is_live"] = ls is not None
                if ls and ls.get("bridge_session_id"):
                    row["bridge_url"] = bridge_url(ls["bridge_session_id"])
                elif row.get("bridge_session_id"):
                    row["bridge_url"] = bridge_url(row["bridge_session_id"])
                else:
                    row["bridge_url"] = None
                if names.get(row["session_id"]):
                    row["title"] = names[row["session_id"]]
                rows.append(row)
                seen.add(row["session_id"])
    # fold in just-opened live sessions that have no jsonl yet
    for sid, s in live.items():
        if sid in seen or not s.get("cwd") or _realpath(s["cwd"]) != target:
            continue
        rows.append(
            {
                "session_id": sid,
                "cwd": s["cwd"],
                "title": names.get(sid) or "(활성 세션)",
                "first_prompt": "",
                "last_activity": (s.get("updated_at") or 0) / 1000.0,
                "msg_count": 0,
                "git_branch": "",
                "is_remote_control": bool(s.get("bridge_session_id")),
                "bridge_session_id": s.get("bridge_session_id"),
                "is_live": True,
                "bridge_url": bridge_url(s["bridge_session_id"]) if s.get("bridge_session_id") else None,
            }
        )
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows


def browse_dir(path: str | None = None) -> dict:
    """List subdirectories of `path` (default: home) so the UI can navigate the
    machine's filesystem and pick a folder. Hidden dirs are omitted. Entries are
    flagged with has_sessions when that folder already holds Claude sessions."""
    home = str(Path.home())
    base = _realpath(path) if path else home
    if not os.path.isdir(base):
        base = home
    session_cwds = {f["cwd"] for f in list_folders()}
    entries = []
    err = None
    try:
        with os.scandir(base) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                full = os.path.join(base, e.name)
                entries.append({"name": e.name, "path": full, "has_sessions": full in session_cwds})
    except OSError as ex:
        err = str(ex)
    entries.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(base) if base != "/" else None
    return {
        "path": base,
        "parent": parent,
        "home": home,
        "has_sessions": base in session_cwds,
        "entries": entries,
        "error": err,
    }


def search_sessions(cwd: str, query: str) -> list[dict]:
    """Search sessions within a folder by title, first prompt, AND conversation
    content. Returns matching rows (same shape as list_sessions_for_cwd) plus a
    `snippet` showing the matched text."""
    rows = list_sessions_for_cwd(cwd)
    q = (query or "").strip().lower()
    if not q:
        return rows
    out = []
    for r in rows:
        snip = _match_snippet(r, q)
        if snip is not None:
            r = dict(r)
            r["snippet"] = snip
            out.append(r)
    return out


def _match_snippet(row: dict, q: str) -> str | None:
    for field in (row.get("title"), row.get("first_prompt")):
        if field and q in field.lower():
            return _snip(field, q)
    f = find_session_file(row.get("session_id", ""))
    if f is None:
        return None
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("user", "assistant") and not obj.get("isMeta"):
                    txt = _text((obj.get("message") or {}).get("content"))
                    if txt and q in txt.lower():
                        return _snip(txt, q)
    except OSError:
        pass
    return None


def _snip(text: str, q: str, width: int = 140) -> str:
    low = text.lower()
    i = low.find(q)
    if i < 0:
        return text[:width]
    start = max(0, i - width // 3)
    end = min(len(text), i + len(q) + width // 2)
    s = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def bridge_url(bridge_session_id: str) -> str:
    """A bridge id may be prefixed session_ (pidfile) or cse_ (jsonl); the
    claude.ai URL uses the session_ form. Normalize the suffix."""
    suffix = bridge_session_id.split("_", 1)[-1]
    return f"https://claude.ai/code/session_{suffix}"


def live_sessions() -> list[dict]:
    """Currently-running sessions, derived from ~/.claude/sessions/<pid>.json.

    Only includes pidfiles whose pid is actually alive (stale files linger)."""
    out = []
    sdir = sessions_dir()
    if not sdir.is_dir():
        return out
    for f in sdir.glob("*.json"):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = obj.get("pid")
        if not pid or not _pid_alive(pid):
            continue
        out.append(
            {
                "pid": pid,
                "session_id": obj.get("sessionId"),
                "cwd": obj.get("cwd"),
                "status": obj.get("status"),
                "bridge_session_id": obj.get("bridgeSessionId"),
                "started_at": obj.get("startedAt", 0),
                "updated_at": obj.get("updatedAt", 0),
            }
        )
    return out


def is_session_live(session_id: str) -> bool:
    return any(s["session_id"] == session_id for s in live_sessions())


def stop_session(session_id: str) -> dict:
    """Terminate a running session by SIGTERM-ing its process (the pidfile pid
    is the re-exec'd claude process). The PTY supervisor then exits on its own."""
    import signal

    for s in live_sessions():
        if s["session_id"] == session_id:
            pid = s["pid"]
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError as e:
                return {"ok": False, "reason": f"kill failed: {e}"}
            return {"ok": True, "pid": pid}
    return {"ok": False, "reason": "not live"}


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows sends CTRL_C_EVENT — never use it for a
        # liveness probe. Query the process via OpenProcess instead.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _realpath(p: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(p))
    except Exception:
        return p


def find_session_file(session_id: str) -> Path | None:
    pdir = projects_dir()
    if not pdir.is_dir():
        return None
    matches = list(pdir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def delete_session(session_id: str) -> dict:
    """Delete a session unless it's live. Removes the jsonl, its companion
    per-session dir (subagents/workflows), and its session-env dir.
    Leaves file-history/ (shared, content-hashed) untouched."""
    if is_session_live(session_id):
        return {"ok": False, "refused": True, "reason": "session is live"}
    f = find_session_file(session_id)
    if f is None:
        # no transcript on disk (e.g. a session opened but never used); sweep any
        # stale pidfile referencing it so it stops showing as a ghost.
        removed = _sweep_stale_pidfiles(session_id)
        if removed:
            return {"ok": True, "removed": removed}
        return {"ok": False, "refused": False, "reason": "not found"}
    removed = []
    try:
        f.unlink()
        removed.append(str(f))
    except OSError as e:
        return {"ok": False, "refused": False, "reason": f"unlink failed: {e}"}
    companion = f.with_suffix("")  # projects/<dir>/<id>/
    _rmtree(companion, removed)
    _rmtree(claude_home() / "session-env" / session_id, removed)
    return {"ok": True, "removed": removed}


def delete_folder_sessions(cwd: str) -> dict:
    """Delete ALL non-live sessions in a folder at once. Live sessions are
    skipped (stop them first). Returns a summary."""
    rows = list_sessions_for_cwd(cwd)
    deleted, skipped, errors = 0, 0, []
    for r in rows:
        if r.get("is_live"):
            skipped += 1
            continue
        res = delete_session(r["session_id"])
        if res.get("ok"):
            deleted += 1
        else:
            errors.append({"id": r["session_id"], "reason": res.get("reason")})
    return {"ok": True, "total": len(rows), "deleted": deleted, "skipped_live": skipped, "errors": errors}


def _sweep_stale_pidfiles(session_id: str) -> list:
    """Remove dead-pid ~/.claude/sessions/<pid>.json files for this session."""
    removed = []
    sdir = sessions_dir()
    if not sdir.is_dir():
        return removed
    for f in sdir.glob("*.json"):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if obj.get("sessionId") == session_id and not _pid_alive(obj.get("pid", -1)):
            try:
                f.unlink()
                removed.append(str(f))
            except OSError:
                pass
    return removed


def _rmtree(path: Path, removed: list):
    if not path.exists():
        return
    import shutil

    try:
        shutil.rmtree(path)
        removed.append(str(path))
    except OSError:
        pass
