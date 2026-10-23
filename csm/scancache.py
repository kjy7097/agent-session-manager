"""Read each transcript once, then only what was appended since.

Listing a folder means summarising every transcript in it, and transcripts
here run to hundreds of MB (Codex's alone were 4.7GB over 643 files, read in
full on every click). Both claude and codex only ever append to them, so the
summary of a file is kept along with how far into it it got: an unchanged file
costs one stat, a live session that grew costs only its new lines, and the
summaries are saved to disk so an agent restart (which auto-update now does on
its own) does not start over.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_caches: dict = {}            # kind -> {path: [size, mtime_ns, offset, state, inode]}
_dirty: set = set()


def _file(kind: str) -> Path:
    from .common import claude_home
    return claude_home() / ".csm-cache" / f"{kind}.json"


def _cache(kind: str) -> dict:
    c = _caches.get(kind)
    if c is None:
        c = {}
        try:
            raw = json.loads(_file(kind).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                c = {p: v for p, v in raw.items() if isinstance(v, list) and len(v) == 5}
        except (OSError, ValueError):
            pass
        _caches[kind] = c
    return c


def feed_lines(fh, state: dict, feed, start: int, with_line: bool = False) -> int:
    """Feed every complete record from `start` on; returns where the next read
    should begin. A last line that does not parse yet is being written and is
    left for next time. with_line: also pass feed the raw line (bytes)."""
    pos = start
    fh.seek(start)
    for line in fh:
        if not line.endswith(b"\n"):
            try:
                obj = json.loads(line)
            except ValueError:
                break
        else:
            s = line.strip()
            if not s:
                pos += len(line)
                continue
            try:
                obj = json.loads(s)
            except ValueError:
                pos += len(line)
                continue
        if isinstance(obj, dict):
            if with_line:
                feed(state, obj, line)
            else:
                feed(state, obj)
        pos += len(line)
    return pos


def scan(kind: str, path: Path, new_state, feed) -> dict | None:
    """The summary `feed` builds over every record of `path`, read
    incrementally. new_state() -> a fresh accumulator (JSON-serialisable)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    with _lock:
        hit = _cache(kind).get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return dict(hit[3])
    try:
        with path.open("rb") as fh:
            state, start = new_state(), 0
            # grown in place: read only the new part. A file replaced under
            # the same name (a move re-points its cwd that way) is a new
            # inode; the byte before the offset must still end a record.
            if hit and hit[4] == st.st_ino and 0 < hit[2] <= st.st_size and st.st_size > hit[0]:
                fh.seek(hit[2] - 1)
                if fh.read(1) == b"\n":
                    state, start = dict(hit[3]), hit[2]
            end = feed_lines(fh, state, feed, start)
    except OSError:
        return None
    with _lock:
        _cache(kind)[key] = [st.st_size, st.st_mtime_ns, end, state, st.st_ino]
        _dirty.add(kind)
    return dict(state)


def flush(kind: str) -> None:
    """Save the summaries if any changed (atomic; files that are gone drop out)."""
    with _lock:
        if kind not in _dirty:
            return
        _dirty.discard(kind)
        c = _cache(kind)
        for p in [p for p in c if not os.path.exists(p)]:
            del c[p]
        data = json.dumps(c, ensure_ascii=False)
    f = _file(kind)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass
