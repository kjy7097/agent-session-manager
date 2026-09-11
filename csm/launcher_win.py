"""Windows ConPTY supervisor for one `claude --remote-control` launch.

The POSIX path uses pty.fork(); Windows has no fork, so we drive a real
pseudo-console via pywinpty. Spawned detached by the agent (DETACHED_PROCESS),
this process owns the ConPTY for the session's lifetime, auto-confirms the
one-time "trust this folder" prompt, then drains output until Claude exits.
The agent discovers the session via ~/.claude/sessions/<pid>.json (it does not
read anything back from here).

Usage:
    python launcher_win.py <folder> <name> <resume_id|''> <0|1 fork> <claude_path>
"""
import os
import re
import sys
import threading
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# The picker's highlight marker: plain ">" on a Windows console, ❯ elsewhere.
POINTERS = ("\u276f", "\u203a", "\u25b6", ">")
NO_OPT = "no,exit"
YES_OPT = "yes,itrustthisfolder"


def _trust_needs_down(norm):
    """Newer claude (>= 2.1.25x) highlights "No, exit" first in the trust dialog,
    so a bare Enter quits. Return True when we must press Down before Enter.

    Checks the character immediately before each option rather than the last
    pointer on screen: escape-sequence leftovers can strand a stray ">".
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
    try:  # keep the token log readable whatever the console codepage is
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    folder = os.path.realpath(os.path.expanduser(sys.argv[1]))
    name = sys.argv[2]
    resume_id = sys.argv[3]
    fork = sys.argv[4] == "1"
    claude_path = sys.argv[5]
    prompt = sys.argv[6] if len(sys.argv) > 6 else ""
    model = sys.argv[7] if len(sys.argv) > 7 else ""
    effort = sys.argv[8] if len(sys.argv) > 8 else ""

    if not os.path.isdir(folder):
        sys.stderr.write(f"launcher_win: folder not found: {folder}\n")
        return 2

    argv = [claude_path, "--remote-control", name]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if resume_id:
        argv += ["-r", resume_id]
        if fork:
            argv += ["--fork-session"]

    from winpty import PtyProcess

    proc = PtyProcess.spawn(argv, cwd=folder, dimensions=(40, 120))

    trust_sent = [0]
    last = [0.0]
    buf = [""]
    ready_at = [None]
    prompt_sent = [not prompt]
    logged = [0]  # mirror the first 64KB of console output to the token log

    def drain():
        while True:
            try:
                data = proc.read(2048)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                time.sleep(0.1)
                continue
            if logged[0] < 65536:
                try:
                    sys.stderr.write(data)
                    sys.stderr.flush()
                except Exception:
                    pass
                logged[0] += len(data)
            buf[0] = (buf[0] + data)[-8000:]
            norm = ANSI.sub("", buf[0]).lower().replace(" ", "").replace("\n", "").replace("\r", "")
            if "trust" in norm and trust_sent[0] < 3 and time.time() - last[0] > 1.5:
                try:
                    time.sleep(0.5)  # let the picker finish mounting
                    if _trust_needs_down(norm):
                        proc.write("\x1b[B")  # move to "Yes, I trust this folder"
                        time.sleep(0.5)
                    proc.write("\r")
                except Exception:
                    pass
                trust_sent[0] += 1
                last[0] = time.time()
                # only retry if claude re-renders the prompt (a 2nd Down would wrap
                # back to "No, exit" and Enter would quit the session)
                buf[0] = ""
            if ready_at[0] is None and ("remotecontrol" in norm or "claude.ai/code" in norm):
                ready_at[0] = time.time()

    t = threading.Thread(target=drain, daemon=True)
    t.start()

    # inject the prompt once the session is ready (interactive, no claude -p)
    start = time.time()
    while not prompt_sent[0]:
        # fallback: assume ready ~8s after start if no READY marker was seen
        if ready_at[0] is None and time.time() - start > 8:
            ready_at[0] = time.time()
        if ready_at[0] and time.time() - ready_at[0] > 4:
            try:
                # bracketed paste so the seed's newlines don't submit line-by-line
                proc.write("\x1b[200~" + prompt + "\x1b[201~")
                time.sleep(0.5)
                proc.write("\r")
            except Exception:
                pass
            prompt_sent[0] = True
        elif not proc.isalive():
            break
        else:
            time.sleep(0.5)

    # keep the ConPTY open for the lifetime of the session
    while proc.isalive():
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
