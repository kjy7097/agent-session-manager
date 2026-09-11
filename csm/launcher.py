"""Detached PTY supervisor for a single `claude --remote-control` launch.

Run as its own session-leader process (start_new_session=True) so the Claude
session survives the agent restarting. It owns the PTY for the lifetime of the
session, auto-confirms the one-time "trust this folder" prompt, then quietly
drains output until Claude exits. The agent does NOT read anything back from
here — it discovers the resulting session by polling ~/.claude/sessions/*.json.

Usage:
    python launcher.py <folder> <name> <resume_id|''> <0|1 fork> <claude_path>
                       [prompt] [model] [effort]

If [prompt] is given, it is typed into the session (PTY stdin) once the session
is ready — interactive, no `claude -p`. The session stays alive afterward.
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
READY_NEEDLES = (b"remotecontrol", b"claude.ai/code")
# The picker's highlight marker: ❯ on POSIX, plain ">" on a Windows console.
POINTERS = ("\u276f", "\u203a", "\u25b6", ">")
NO_OPT = "no,exit"
YES_OPT = "yes,itrustthisfolder"


def _trust_needs_down(norm: str) -> bool:
    """Newer claude (>= 2.1.25x) highlights "No, exit" first in the trust dialog,
    so a bare Enter quits. Return True when we must press Down before Enter.

    `norm` is the screen text lowercased with spaces/newlines stripped. We look at
    the character immediately before each option rather than the last pointer on
    screen, because escape-sequence leftovers can strand a stray ">".
    """
    i = norm.rfind(NO_OPT)
    j = norm.rfind(YES_OPT)
    if i == -1 or j == -1:
        return False  # not the newer two-option dialog: keep the old bare Enter
    if i and norm[i - 1] in POINTERS:
        return True
    if j and norm[j - 1] in POINTERS:
        return False
    return True  # dialog is up but no marker seen: newer claude defaults to "No"


def main() -> int:
    folder = sys.argv[1]
    name = sys.argv[2]
    resume_id = sys.argv[3]
    fork = sys.argv[4] == "1"
    claude_path = sys.argv[5]
    prompt = sys.argv[6] if len(sys.argv) > 6 else ""
    model = sys.argv[7] if len(sys.argv) > 7 else ""
    effort = sys.argv[8] if len(sys.argv) > 8 else ""

    folder = os.path.realpath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        sys.stderr.write(f"launcher: folder does not exist: {folder}\n")
        return 2

    argv = ["claude", "--remote-control", name]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
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

    # parent: drain pty, auto-confirm trust, optionally inject a prompt, stay alive
    buf = b""
    trust_sent = 0
    last_trust = 0.0
    start = time.monotonic()
    ready_at = None
    prompt_sent = not prompt  # nothing to send if no prompt
    logged = 0  # mirror the first 64KB of PTY output to stderr (the token log)
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
            if logged < 65536:
                try:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
                except (OSError, ValueError):
                    pass
                logged += len(data)
            buf += data
            norm = ANSI.sub(b"", buf).lower().replace(b" ", b"").replace(b"\n", b"")
            if (
                TRUST_NEEDLE in norm
                and trust_sent < 3
                and time.monotonic() - last_trust > 1.5
            ):
                try:
                    time.sleep(0.5)  # let the picker finish mounting
                    if _trust_needs_down(norm.decode("utf-8", "replace")):
                        os.write(fd, b"\x1b[B")  # move to "Yes, I trust this folder"
                        time.sleep(0.5)
                    os.write(fd, b"\r")
                except OSError:
                    pass
                trust_sent += 1
                last_trust = time.monotonic()
                # forget the dialog text we just answered: a retry must only fire
                # if claude re-renders the prompt (otherwise a 2nd Down wraps back
                # to "No, exit" and Enter quits the session)
                buf = b""
            if ready_at is None and any(n in norm for n in READY_NEEDLES):
                ready_at = time.monotonic()
            buf = buf[-8192:]  # bound memory over a long session
        # fallback: if we never saw a READY marker (claude may emit little on the
        # PTY), assume ready ~8s after start so the seed still gets injected.
        if not prompt_sent and ready_at is None and time.monotonic() - start > 8:
            ready_at = time.monotonic()
        # inject the prompt a few seconds after the session is ready. Wrap it in
        # a bracketed paste so the seed's newlines don't each submit as Enter.
        if not prompt_sent and ready_at and time.monotonic() - ready_at > 4:
            try:
                data = b"\x1b[200~" + prompt.encode() + b"\x1b[201~"
                for i in range(0, len(data), 1024):   # chunk to avoid PTY limits
                    os.write(fd, data[i:i + 1024])
                    time.sleep(0.02)
                time.sleep(0.5)
                os.write(fd, b"\r")
            except OSError:
                pass
            prompt_sent = True
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
