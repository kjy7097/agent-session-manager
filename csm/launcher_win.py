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


def main() -> int:
    folder = os.path.realpath(os.path.expanduser(sys.argv[1]))
    name = sys.argv[2]
    resume_id = sys.argv[3]
    fork = sys.argv[4] == "1"
    claude_path = sys.argv[5]
    prompt = sys.argv[6] if len(sys.argv) > 6 else ""
    model = sys.argv[7] if len(sys.argv) > 7 else ""

    if not os.path.isdir(folder):
        sys.stderr.write(f"launcher_win: folder not found: {folder}\n")
        return 2

    argv = [claude_path, "--remote-control", name]
    if model:
        argv += ["--model", model]
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
            buf[0] = (buf[0] + data)[-8000:]
            norm = ANSI.sub("", buf[0]).lower().replace(" ", "").replace("\n", "").replace("\r", "")
            if "trust" in norm and trust_sent[0] < 3 and time.time() - last[0] > 1.5:
                try:
                    proc.write("\r")
                except Exception:
                    pass
                trust_sent[0] += 1
                last[0] = time.time()
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
