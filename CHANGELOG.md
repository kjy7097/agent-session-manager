# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.2.0
[0.1.0]: https://github.com/kjy7097/agent-session-manager/releases/tag/v0.1.0
