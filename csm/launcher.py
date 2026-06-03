"""Detached PTY supervisor for a single `claude --remote-control` launch.

Run as its own session-leader process (start_new_session=True) so the Claude
session survives the agent restarting. It owns the PTY for the lifetime of the
session, auto-confirms the one-time "trust this folder" prompt, then quietly
drains output until Claude exits. The agent does NOT read anything back from
here — it discovers the resulting session by polling ~/.claude/sessions/*.json.

Usage:
    python launcher.py <folder> <name> <resume_id|''> <0|1 fork> <claude_path>
"""

from __future__ import annotations

import os
import pty
import re
import select
import sys
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
TRUST_NEEDLE = b"trustthisfolder"  # normalized (no spaces/newlines, lowercase)


def main() -> int:
    folder = sys.argv[1]
    name = sys.argv[2]
    resume_id = sys.argv[3]
    fork = sys.argv[4] == "1"
    claude_path = sys.argv[5]

    folder = os.path.realpath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        sys.stderr.write(f"launcher: folder does not exist: {folder}\n")
        return 2

    argv = ["claude", "--remote-control", name]
    if resume_id:
        argv += ["-r", resume_id]
        if fork:
            argv += ["--fork-session"]

    pid, fd = pty.fork()
    if pid == 0:
        # child
        os.chdir(folder)
        os.environ["TERM"] = "xterm-256color"
        os.execv(claude_path, argv)
        os._exit(127)  # unreachable on success

    # parent: drain pty, auto-confirm trust, stay alive for the session
    buf = b""
    trust_sent = 0
    last_trust = 0.0
    start = time.monotonic()
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError):
            break
        if r:
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break  # claude exited
            buf += data
            norm = ANSI.sub(b"", buf).lower().replace(b" ", b"").replace(b"\n", b"")
            if (
                TRUST_NEEDLE in norm
                and trust_sent < 3
                and time.monotonic() - last_trust > 1.5
            ):
                try:
                    os.write(fd, b"\r")
                except OSError:
                    pass
                trust_sent += 1
                last_trust = time.monotonic()
            buf = buf[-8192:]  # bound memory over a long session
        # safety: if claude never produced output and is gone, exit
        if time.monotonic() - start > 5 and not _alive(pid):
            break

    try:
        os.close(fd)
    except OSError:
        pass
    return 0


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
