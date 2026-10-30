"""The searchable text of each transcript, kept beside it.

A transcript is mostly not words anyone searches for: pasted images, the full
API request claude logs on every turn (system prompt, skill list), attachments
and file snapshots. Measured here, the conversation, thinking and tool calls
are about 2% of the bytes. Searching meant reading every byte of every
transcript (5GB on one PC, 11-14s per search), and the API-request copies made
a common word match every session.

So the text is pulled out once into ~/.claude/.csm-cache/text/, one line per
record, and kept up to date the way scancache keeps its summaries: only what
was appended since is read. Each line starts with a kind and a tab:
  C  conversation — user/assistant text, what the folder search looks at
  X  the rest worth finding — thinking, tool input, tool output, meta prompts
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from . import scancache

_lock = threading.Lock()
_path_locks: dict = {}
_meta: dict = {}             # path -> [size, mtime_ns, offset, inode, text_len]
_loaded = False
_dirty = False


def _dir() -> Path:
    from .common import claude_home
    return claude_home() / ".csm-cache" / "text"


def _meta_file() -> Path:
    return _dir().parent / "text.json"


def _load():
    global _loaded, _meta
    if _loaded:
        return
    try:
        raw = json.loads(_meta_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _meta = {p: v for p, v in raw.items() if isinstance(v, list) and len(v) == 5}
    except (OSError, ValueError):
        pass
    _loaded = True


def text_file(path: Path) -> Path:
    tag = hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:8]
    return _dir() / f"{path.stem}-{tag}.txt"


def _flat(s: str) -> str:
    return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _strings(content):
    """Text out of a content field, leaving images and the like behind."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                yield b.get("text") or ""


def _values(v, depth: int = 0):
    """The strings inside a tool call's input, as written — not JSON-escaped,
    so a snippet of an edit reads as code, not as \\n and \\\"."""
    if depth > 6:
        return
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _values(x, depth + 1)
    elif isinstance(v, list):
        for x in v:
            yield from _values(x, depth + 1)


def _emit(out, kind: str, s: str):
    s = _flat(s).strip()
    if s:
        out.write(f"{kind}\t{s}\n".encode("utf-8", "replace"))


def _feed(st: dict, obj: dict) -> None:
    out = st["out"]
    t = obj.get("type")
    if t == "ai-title":
        _emit(out, "X", obj.get("aiTitle") or "")
        return
    if t not in ("user", "assistant"):
        return              # api-request copies, attachments, snapshots, ...
    content = (obj.get("message") or {}).get("content")
    kind = "X" if obj.get("isMeta") else "C"
    _emit(out, kind, "".join(_strings(content)))
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            _emit(out, "X", b.get("thinking") or "")
        elif bt == "tool_use":
            _emit(out, "X", " ".join(_values(b.get("input"))))
        elif bt == "tool_result":
            c = b.get("content")
            _emit(out, "X", c if isinstance(c, str) else " ".join(_strings(c)))


def ensure(path: Path) -> Path | None:
    """Bring the text file of one transcript up to date; returns it."""
    global _dirty
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    with _lock:
        _load()
        plock = _path_locks.setdefault(key, threading.Lock())
    with plock:
        with _lock:
            hit = _meta.get(key)
        tf = text_file(path)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns and tf.exists():
            return tf
        try:
            tf.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as fh:
                start, keep = 0, 0
                # grown in place: carry on from where the last pass stopped
                # (same test as scancache: same inode, a record ends there)
                if (hit and hit[3] == st.st_ino and 0 < hit[2] <= st.st_size
                        and st.st_size > hit[0] and tf.exists()):
                    fh.seek(hit[2] - 1)
                    if fh.read(1) == b"\n":
                        start, keep = hit[2], hit[4]
                with tf.open("r+b" if keep else "wb") as out:
                    out.truncate(keep)      # drop anything written past the record
                    out.seek(keep)
                    end = scancache.feed_lines(fh, {"out": out}, _feed, start)
                    length = out.tell()
        except OSError:
            return None
        with _lock:
            _meta[key] = [st.st_size, st.st_mtime_ns, end, st.st_ino, length]
            _dirty = True
        return tf


def flush() -> None:
    global _dirty
    with _lock:
        if not _dirty:
            return
        _dirty = False
        gone = [p for p in _meta if not os.path.exists(p)]
        for p in gone:
            try:
                text_file(Path(p)).unlink()
            except OSError:
                pass
            del _meta[p]
        data = json.dumps(_meta, ensure_ascii=False)
    f = _meta_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


def lines(path: Path):
    """(kind, text) for every line of a transcript's text, brought up to date."""
    tf = ensure(path)
    if tf is None:
        return
    try:
        with tf.open("rb") as fh:
            for raw in fh:
                s = raw.decode("utf-8", "replace").rstrip("\n")
                k, _, txt = s.partition("\t")
                yield k, txt
    except OSError:
        return


def has(path: Path, needles: list, loose: list) -> bool:
    """Whether any needle (lowercased bytes) is in the transcript's text;
    `loose` needles are matched with spaces squeezed out."""
    tf = ensure(path)
    if tf is None:
        return False
    try:
        data = tf.read_bytes().lower()
    except OSError:
        return False
    if any(n in data for n in needles):
        return True
    if loose:
        flat = data.replace(b" ", b"")
        return any(n in flat for n in loose)
    return False
