# Agent Session Manager

**English** | [한국어](README.ko.md)

A self-hosted web control center for **CLI coding-agent sessions across multiple
machines**. It drives **Claude Code** (`claude --remote-control`) and **OpenAI
Codex** behind one control plane — folders and session lists merge both agents,
with per-agent actions on each session.

Open a remote session in any folder on any of your computers, browse / search /
resume / delete past sessions per folder, and keep your custom skills in sync —
all from one browser tab. No more SSHing into each box and typing
`claude --remote-control` by hand.

> Works over **any SSH-reachable network** — LAN, a VPN/mesh (e.g. Tailscale), or
> public IPs. There is **no dependency on Tailscale**; a machine is just a host +
> SSH user.

## Features

- **Multi-machine** — register machines by host + SSH user (Windows **and Linux**
  targets); reach each agent over an auto-reconnecting SSH tunnel.
- **Multi-agent** — Claude Code and OpenAI Codex sessions side by side
  (🟧 / 🟢 badges), one unified "＋ New session" button — then pick the agent.
- **Claude sessions** open in a `claude.ai/code` tab (bridge URL); resume / fork /
  stop / delete.
- **Codex sessions** run through the **Codex app-server** so they also appear in
  the **Codex desktop app** (see below). Continue a session in the in-app **chat
  panel** or in an **interactive terminal**, preview the transcript, delete.
- **Terminal hand-off** — open a codex TUI on **the PC your browser is on**
  (the control plane detects the client machine by its tailnet IP; remote targets
  are reached with `ssh -t`).
- **Model selection** — choose a model per new session / per resume, and set a
  server-saved default from the header.
- **Rewind & cross-agent switch** — branch a new session from any point in a
  transcript, or continue the conversation in the *other* agent (🟧 Claude ↔ 🟢 Codex).
- **Browse by folder**, per machine; **search sessions** by title *and conversation
  content*; **preview** a transcript before resuming.
- **Automatic skill sync** — your `~/.claude/skills` converge to the newest version
  of each skill across all machines (last-write-wins by mtime), excluding
  credential/encrypted files. Edit anywhere; it propagates.
- **Survives reboots** — agents run as OS services (Windows Task Scheduler /
  Linux systemd / macOS launchd); the control plane + tunnels run under launchd.
- **Korean/English UI** — 🌐 toggle; the preference persists server-side.
- Single static web UI (vanilla JS), mobile-friendly. Pure Python 3 standard
  library on the backend (plus `pywinpty` on Windows agents).

## Codex desktop app integration

ASM creates Codex sessions through the **Codex app-server** (`thread/start` +
`turn/start`), so they are tagged `source=vscode` and **show up in the Codex
desktop app's project list** — a `codex exec` session would be `source=exec` and
the app hides it. The app picks new sessions up on its next poll (no reconnect
needed).

One thing ASM **cannot** do: **add a project/folder to the desktop app.** The app
keeps its project list internally (not in any file ASM can write), so:

1. **Add the folder once in the Codex desktop app** (as a project).
2. ASM then lists that folder under **`/codex/projects`** and creates sessions in
   it — and those sessions appear in the app under that project.

In other words: **projects are added in the app (one-time, manual); sessions are
created by ASM (unlimited, automatic).** There is no "open in app" button for
Codex — the app can't be driven from outside — but sessions ASM creates simply
appear there.

## Architecture

```
browser ──► control plane (this host) ──► SSH tunnel ──► agent (each machine)
            registry + UI + auth proxy                   ~/.claude + ~/.codex
```

- **agent** (`csm/agent.py`) — one per machine. Reads that machine's `~/.claude`
  **and `~/.codex`** (folders, sessions, transcripts, skills), launches detached
  `claude --remote-control` sessions in a real PTY (POSIX `pty.fork`, or ConPTY via
  `pywinpty` on Windows), and opens codex terminals on the machine's desktop.
  Bound to `127.0.0.1`; bearer-token (HMAC) auth.
- **codex adapter** (`csm/codex.py`) — parses Codex rollout JSONLs, drives the
  **codex app-server** (`thread/start` / `turn/start` / `thread/resume`) so sessions
  are app-visible, and reads `config.toml [projects]`.
- **control plane** (`csm/control.py`) — serves the web UI, holds the machine
  registry (from `csm.config.json`), proxies browser requests to each agent over
  its local SSH-tunnel port (attaching the per-machine secret), and routes the
  terminal hand-off to the right machine by client IP.
- **tunnels** (`csm/tunnels.py`) — keeps a forward-only SSH tunnel
  (`localPort → host:agentPort`) alive per machine.
- **skill sync** (`csm/skillsync.py`) — periodic per-skill convergence across machines.

## Requirements

- **Control host**: Python 3.10+, OpenSSH client, an SSH key.
- **Each machine**: Claude Code CLI installed and logged in (`claude auth`),
  Python 3, OpenSSH server, and the control host's SSH public key authorized.
  For Codex features: the Codex CLI installed and signed in (ChatGPT login or a
  local provider) — optional per machine.
- Windows agents additionally use `pywinpty` (installed automatically by the
  deploy script).

## Quick start

```bash
git clone <this-repo> && cd agent-session-manager
./run.sh                      # starts the local agent + control plane
# open http://127.0.0.1:8765
```

On first run a `csm.config.json` is created (random secret). See
`csm.config.example.json` for the schema. To expose it to your other devices,
bind the control plane to your VPN/LAN IP in `csm.config.json` (`control.host`).

### Add a machine

1. Authorize the control host's SSH key on the target (one-time), with the
   enroll script served by the control plane:
   - **Windows** (elevated PowerShell):
     ```powershell
     irm http://<control-host>:8765/enroll | iex
     ```
   - **Linux** (terminal; sudo will prompt):
     ```bash
     curl -fsSL http://<control-host>:8765/enroll?os=linux | bash
     ```
   (sets up the OpenSSH server, authorizes the key, checks Python).
2. From the control host, deploy the agent:
   ```bash
   python scripts/deploy_agent.py <id> <host> <ssh-user>
   ```
   The target OS is auto-detected; the agent autostarts via a Windows scheduled
   task or a Linux systemd user service, a tunnel opens, and the machine lands in
   `csm.config.json`. Refresh the UI — it appears. (Or use the **Add machine**
   dialog in the UI, which does the same.)

## Security

- Agents bind to `127.0.0.1` and are reached only via SSH tunnels; each agent
  requires a per-machine bearer secret. The web UI/secret never leave the control
  host. **Keep the control plane on a trusted network** (localhost/VPN/LAN) — an
  agent can launch processes in arbitrary folders, so treat access as you would
  shell access.
- `csm.config.json` holds secrets and is git-ignored. Never commit it.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Current version: **0.5.0**.

## License

MIT — see [LICENSE](LICENSE).
