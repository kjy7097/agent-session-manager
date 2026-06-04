# Agent Session Manager

A self-hosted web control center for **CLI coding-agent sessions across multiple
machines**. It currently drives **Claude Code** (`claude --remote-control`) and is
structured so other CLI agents can be added behind the same control plane.

Open a remote session in any folder on any of your computers, browse
/ search / resume / delete past sessions per folder, and keep your custom skills
in sync — all from one browser tab. No more SSHing into each box and typing
`claude --remote-control` by hand.

> Works over **any SSH-reachable network** — LAN, a VPN/mesh (e.g. Tailscale), or
> public IPs. There is **no dependency on Tailscale**; a machine is just a host +
> SSH user.

## Features

- **Multi-machine** — register machines by host + SSH user; reach each agent over
  an auto-reconnecting SSH tunnel.
- **Open sessions in any folder** with zero manual SSH/cd — pick (or pin) a folder,
  click "new session"; the bridge URL (`claude.ai/code/...`) opens in a new tab.
- **Browse by folder**, per machine; **search sessions** by title *and conversation
  content*; **preview** a transcript before resuming.
- **Resume / Fork**, **stop** a running session, **delete** a session or a whole
  folder's sessions.
- **Automatic skill sync** — your `~/.claude/skills` converge to the newest version
  of each skill across all machines (last-write-wins by mtime), excluding
  credential/encrypted files. Edit anywhere; it propagates.
- **Survives reboots** — agents run as OS services (Windows Task Scheduler /
  macOS launchd); the control plane + tunnels run under launchd.
- Single static web UI (vanilla JS), mobile-friendly. Pure Python 3 standard
  library on the backend (plus `pywinpty` on Windows agents).

## Architecture

```
browser ──► control plane (this host) ──► SSH tunnel ──► agent (each machine)
            registry + UI + auth proxy                   ~/.claude reads + PTY launch
```

- **agent** (`csm/agent.py`) — one per machine. Reads that machine's `~/.claude`
  (folders, sessions, transcripts, skills) and launches detached
  `claude --remote-control` sessions in a real PTY (POSIX `pty.fork`, or ConPTY via
  `pywinpty` on Windows). Bound to `127.0.0.1`; bearer-token (HMAC) auth.
- **control plane** (`csm/control.py`) — serves the web UI, holds the machine
  registry (from `csm.config.json`), and proxies browser requests to each agent
  over its local SSH-tunnel port (attaching the per-machine secret).
- **tunnels** (`csm/tunnels.py`) — keeps a forward-only SSH tunnel
  (`localPort → host:agentPort`) alive per machine.
- **skill sync** (`csm/skillsync.py`) — periodic per-skill convergence across machines.

## Requirements

- **Control host**: Python 3.10+, OpenSSH client, an SSH key.
- **Each machine**: Claude Code CLI installed and logged in (`claude auth`),
  Python 3, OpenSSH server, and the control host's SSH public key authorized.
- Windows agents additionally use `pywinpty` (installed automatically by the
  deploy script).

## Quick start

```bash
git clone <this-repo> && cd claude-session-manager
./run.sh                      # starts the local agent + control plane
# open http://127.0.0.1:8765
```

On first run a `csm.config.json` is created (random secret). See
`csm.config.example.json` for the schema. To expose it to your other devices,
bind the control plane to your VPN/LAN IP in `csm.config.json` (`control.host`).

### Add a machine

1. Authorize the control host's SSH key on the target (one-time). On Windows,
   run an **elevated** PowerShell command served at `http://<control>/enroll`:
   ```powershell
   irm http://<control-host>:8765/enroll | iex
   ```
   (sets up OpenSSH server, authorizes the key, checks Python).
2. From the control host, deploy the agent:
   ```bash
   python scripts/deploy_agent.py <id> <host> <ssh-user>
   ```
   This pushes the agent, registers an autostart service, opens a tunnel, and adds
   the machine to `csm.config.json`. Refresh the UI — it appears.

## Security

- Agents bind to `127.0.0.1` and are reached only via SSH tunnels; each agent
  requires a per-machine bearer secret. The web UI/secret never leave the control
  host. **Keep the control plane on a trusted network** (localhost/VPN/LAN) — an
  agent can launch processes in arbitrary folders, so treat access as you would
  shell access.
- `csm.config.json` holds secrets and is git-ignored. Never commit it.

## Changelog

- **Fix — live-session count:** Claude re-execs into a versioned binary, so one
  session can leave several alive `~/.claude/sessions/<pid>.json` files. The live
  count now dedupes by `sessionId`, so a folder shows one "live" per session
  (previously a single re-exec'd session was miscounted as 2+).

## License

MIT — see [LICENSE](LICENSE).
