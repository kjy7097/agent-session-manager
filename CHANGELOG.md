# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-06-11

### Changed
- **Codex now runs through the Codex *app-server*** (`thread/start` + `turn/start`)
  instead of `codex exec`. Sessions created this way are tagged `source=vscode`
  with a preview, so they **show up in the Codex desktop app's project list**
  (an `codex exec` session is `source=exec` and the app filters it out). The app
  picks new sessions up on its next poll — no reconnect needed.

### Added
- **Terminal hand-off** — a codex session opens an interactive TUI on the PC your
  browser is on (the control plane detects the client machine by its tailnet IP;
  the mac opens Terminal.app, a remote PC opens a console via its own agent; the
  session runs locally or via `ssh -t` to the target).
- **Model selection** — pick a model per new session / per resume, and set a
  server-saved default from the header (Claude: fable / opus / sonnet / haiku;
  Codex: `gpt-5.5` etc.).
- **Per-session model badge** in the session list and transcript.
- **`codex.list_projects()` / `/codex/projects`** — the folders Codex knows as
  projects (`config.toml [projects]`), surfaced so new codex sessions land in a
  folder the desktop app already shows.

### Notes
- Adding a *project/folder* to the Codex desktop app must still be done **in the
  app** (it keeps that list internally; it is not writable from outside). Add the
  folder once in the app, then ASM sessions in that folder appear under it.

## [0.4.0] - 2026-06-09

### Added
- **Rewind & model switch from the transcript.** Each message in the session
  viewer has a ↶ button that branches a **new session from that point**, seeded
  with the context up to there. Footer buttons continue the whole conversation in
  the **other agent** (🟧 Claude ↔ 🟢 Codex). Both work cross-agent because the
  history is carried over as text into a fresh target session (`/handoff`).

## [0.3.1] - 2026-06-10

### Added
- **Korean/English UI** — 🌐 toggle in the header; the preference is saved
  server-side (`/api/prefs`) and the first visit follows the browser language.

### Fixed
- Codex sessions now have a Delete button (removes the rollout file).

## [0.3.0] - 2026-06-09

### Added
- **Codex CLI support — unified multi-agent session manager.** Folders and
  session lists now merge **Claude Code and OpenAI Codex** sessions per
  machine/folder, with 🟧 claude / 🟢 codex badges. (`csm/codex.py` adapter
  reads `~/.codex/sessions` rollouts.)
- **Codex chat** — open a Codex session in a 💬 chat modal: read the
  transcript and continue the conversation turn-by-turn
  (`codex exec resume`, non-interactive; uses the machine's own codex login
  or local provider).
- **Image input for Codex** — attach files (📎) or paste images from the
  clipboard into the chat; images upload to the target machine and pass via
  `codex exec -i`.
- **Unified "＋ New session"** — one button, then choose the agent
  (🟧 Claude / 🟢 Codex).
- **Linux machines** — enroll script (`curl .../enroll?os=linux | bash`) and
  agent deploy via a systemd user service (+ linger); deploy auto-detects the
  target OS. Verified on Ubuntu 22.04.
- **Folder picker upgrades** — Windows drive list as a side panel, and the
  path breadcrumb segments are clickable.

### Fixed
- Add-machine now respects a custom SSH port end-to-end (it previously
  deployed to port 22 regardless), and first connections auto-accept the
  host key.
- Deploy tolerates non-UTF-8 (e.g. CP949) output from Windows targets.

## [0.2.0] - 2026-06-04

### Added
- **Manager session** — open a persistent manager session from a fixed bar at the
  top of the sidebar, with **reset** (end it and start fresh) and **delete**
  (clear its history). Served via `/api/manager` (`?fresh=1`, `?delete=1`).
- **Session transcript view** (`/transcript`) — read a session's recent messages,
  read-only, without touching the running session.
- **Folder picker file preview** — the picker now lists the files in the browsed
  folder (name + size) so you can confirm it's the right one before adding it.
- **Pin to top** — pin folders (per machine) and sessions (📌); pinned items sort
  to the top of their list and stay visible above the "show more" fold.

### Changed
- "Show more / less" folder toggle is now sticky to the bottom of the list, so a
  long folder list can be collapsed without scrolling to the very end.
- Windows: the launcher no longer flashes a console window when a session starts
  (`pythonw` + `CREATE_NO_WINDOW`).

## [0.1.0] - 2026-06-03

### Added
- Initial release — a web control center for Claude Code (`--remote-control`)
  sessions across machines, over SSH.
- List nodes, folders, and sessions; open live sessions via their
  `claude.ai/code` URL; resume / fork / delete.
- Register machines over SSH with one-step agent deploy. English web UI. MIT.
- Live-session count dedupes by `sessionId` (a re-exec'd session is counted once).

[0.5.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.5.0
[0.4.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.4.0
[0.3.1]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.3.1
[0.3.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.3.0
[0.2.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.2.0
[0.1.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.1.0
