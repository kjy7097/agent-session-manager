"""Move a Claude session to another machine and/or folder.

Claude resumes a session from nothing but its transcript: copy
projects/<dir>/<id>.jsonl (and its companion <id>/ folder — subagents,
tool-results, title) to another machine, `claude --resume <id>` there, and it
carries on, even from macOS to Windows. What does not carry over is the folder:
every record names the cwd it ran in, and the manager files a session under the
first one. So on the way in, records that ran in the old folder (or below it)
are re-pointed at the new one; everything else in the transcript is left as is.

The old copy is not deleted. It moves to ~/.claude/.csm-moved/<id>/, out of
sight of both claude and the manager, so one id is never live in two places.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import time
import uuid
from pathlib import Path

from . import common

_CWD = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _valid_id(sid: str) -> bool:
    try:
        return str(uuid.UUID(sid)) == sid.lower()
    except (ValueError, AttributeError):
        return False


def moved_dir() -> Path:
    return common.claude_home() / ".csm-moved"


def dir_name(cwd: str) -> str:
    """The projects/ folder claude itself would use for this cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _stage(session_id: str) -> Path:
    # outside projects/, or the half-unpacked copy would show up as a folder
    stage = common.claude_home() / ".csm-incoming" / session_id
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    return stage


def _project_dir_for(cwd: str) -> Path:
    """Reuse the folder that already holds this cwd's sessions (claude shortens
    and hashes very long names), else claude's own naming."""
    target = common._realpath(cwd)
    pdir = common.projects_dir()
    if pdir.is_dir():
        for d in pdir.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                fc = common._first_cwd(f)
                if fc and common._realpath(fc) == target:
                    return d
                break
    return pdir / dir_name(cwd)


def _sep(path: str) -> str:
    return "\\" if re.match(r"^[A-Za-z]:\\", path) or ("\\" in path and "/" not in path) else "/"


def _remapper(src: str, dst: str):
    """old cwd (or a folder under it) -> the same place under the new cwd."""
    s_sep, d_sep = _sep(src), _sep(dst)
    win = s_sep == "\\"
    src_n = src.rstrip(s_sep)
    key = src_n.lower() if win else src_n

    def remap(p: str) -> str | None:
        k = p.lower() if win else p
        if k == key:
            return dst
        if k.startswith(key + s_sep):
            rest = [x for x in p[len(src_n) + 1:].split(s_sep) if x]
            return dst.rstrip(d_sep) + d_sep + d_sep.join(rest) if rest else dst
        return None
    return remap


def _rewrite(path: Path, remap) -> int:
    """Re-point the cwd of every record in one transcript. Touches only the
    cwd value; the rest of each line is copied byte for byte."""
    tmp = path.with_suffix(".jsonl.csm-tmp")
    n = 0

    def sub(m):
        nonlocal n
        try:
            old = json.loads(b'"' + m.group(1) + b'"')
        except ValueError:
            return m.group(0)
        new = remap(old)
        if new is None:
            return m.group(0)
        n += 1
        return b'"cwd":' + json.dumps(new, ensure_ascii=False).encode("utf-8")

    with path.open("rb") as src, tmp.open("wb") as out:
        for line in src:
            if b'"cwd"' in line:
                line = _CWD.sub(sub, line)
            out.write(line)
    os.replace(tmp, path)
    return n


def info(session_id: str, cwd: str = "") -> dict:
    """What the mover needs to know before it starts, on either end."""
    if not _valid_id(session_id):
        return {"error": "bad session id"}
    f = common.find_session_file(session_id)
    out = {"found": f is not None, "live": common.is_session_live(session_id)}
    if f is not None:
        comp = f.with_suffix("")
        size = f.stat().st_size
        if comp.is_dir():
            size += sum(p.stat().st_size for p in comp.rglob("*") if p.is_file())
        out.update(cwd=common._first_cwd(f) or "", size=size,
                   name=common.session_names().get(session_id, ""))
    if cwd:
        real = common._realpath(cwd)
        out.update(target=real, dir_ok=os.path.isdir(real))
    return out


def export(session_id: str, out_path: Path) -> dict:
    """Pack a transcript and its companion folder as a .tar.gz."""
    if not _valid_id(session_id):
        return {"error": "bad session id"}
    if common.is_session_live(session_id):
        return {"error": "live"}
    f = common.find_session_file(session_id)
    if f is None:
        return {"error": "not found"}
    with tarfile.open(out_path, "w:gz", compresslevel=1) as tar:
        tar.add(str(f), arcname=f.name)
        comp = f.with_suffix("")
        if comp.is_dir():
            tar.add(str(comp), arcname=session_id)
    return {"ok": True}


def _safe_members(tar: tarfile.TarFile, session_id: str):
    for m in tar.getmembers():
        name = m.name.replace("\\", "/")
        parts = name.split("/")
        if ".." in parts or name.startswith("/") or not (m.isfile() or m.isdir()):
            continue
        if parts[0] not in (session_id, session_id + ".jsonl"):
            continue
        yield m


def archive(session_id: str, note: dict | None = None) -> dict:
    """Put a session out of sight (not deleted) — the moved-away copy."""
    if not _valid_id(session_id):
        return {"error": "bad session id"}
    if common.is_session_live(session_id):
        return {"error": "live"}
    f = common.find_session_file(session_id)
    if f is None:
        return {"ok": True, "archived": False}
    dest = moved_dir() / session_id
    if dest.exists():
        dest = moved_dir() / f"{session_id}-{int(time.time())}"
    dest.mkdir(parents=True)
    comp = f.with_suffix("")
    shutil.move(str(f), str(dest / f.name))
    if comp.is_dir():
        shutil.move(str(comp), str(dest / session_id))
    meta = dict(note or {})
    meta.update(fromCwd=common._first_cwd(dest / f.name) or "", at=time.time())
    (dest / "moved.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "archived": True, "to": str(dest)}


def _install(session_id: str, staged_jsonl: Path, staged_comp: Path | None,
             src_cwd: str, cwd: str, name: str) -> dict:
    real = common._realpath(cwd)
    remap = _remapper(src_cwd, real) if src_cwd else None
    changed = 0
    if remap:
        changed += _rewrite(staged_jsonl, remap)
        if staged_comp and staged_comp.is_dir():
            for j in staged_comp.rglob("*.jsonl"):
                changed += _rewrite(j, remap)
    # a copy already here (moved away earlier, now coming back) is the stale one
    if common.find_session_file(session_id) is not None:
        r = archive(session_id, {"reason": "replaced by an incoming move"})
        if r.get("error"):
            return r
    dest = _project_dir_for(real)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_jsonl), str(dest / f"{session_id}.jsonl"))
    if staged_comp and staged_comp.is_dir():
        shutil.move(str(staged_comp), str(dest / session_id))
    if name:
        common.set_session_name(session_id, name)
    return {"ok": True, "cwd": real, "dir": str(dest), "rewritten": changed}


def import_tar(session_id: str, tar_path: Path, src_cwd: str, cwd: str, name: str = "") -> dict:
    if not _valid_id(session_id):
        return {"error": "bad session id"}
    if not os.path.isdir(common._realpath(cwd)):
        return {"error": f"folder not found: {cwd}"}
    if common.is_session_live(session_id):
        return {"error": "live"}
    stage = _stage(session_id)
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(stage, members=list(_safe_members(tar, session_id)))
        j = stage / f"{session_id}.jsonl"
        if not j.is_file():
            return {"error": "transcript missing from the package"}
        comp = stage / session_id
        return _install(session_id, j, comp if comp.is_dir() else None, src_cwd, cwd, name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def relocate(session_id: str, cwd: str) -> dict:
    """Same machine, different folder: no copy over the wire."""
    if not _valid_id(session_id):
        return {"error": "bad session id"}
    if not os.path.isdir(common._realpath(cwd)):
        return {"error": f"folder not found: {cwd}"}
    if common.is_session_live(session_id):
        return {"error": "live"}
    f = common.find_session_file(session_id)
    if f is None:
        return {"error": "not found"}
    src_cwd = common._first_cwd(f) or ""
    stage = _stage(session_id)
    comp = f.with_suffix("")
    staged_comp = stage / session_id
    try:
        j = stage / f.name
        shutil.copyfile(f, j)          # the original stays put until the new one is in
        if comp.is_dir():
            shutil.move(str(comp), str(staged_comp))
        name = common.session_names().get(session_id, "")
        return _install(session_id, j, staged_comp, src_cwd, cwd, name)
    finally:
        # failed half way: the companion folder goes back where it was
        if staged_comp.is_dir() and not comp.exists():
            shutil.move(str(staged_comp), str(comp))
        shutil.rmtree(stage, ignore_errors=True)
