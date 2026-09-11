# Agent Session Manager

[English](README.md) | **한국어**

여러 대의 컴퓨터에 흩어진 **CLI 코딩 에이전트 세션**을 한 브라우저 탭에서
관리하는 자체 호스팅 웹 컨트롤 센터입니다. **Claude Code**
(`claude --remote-control`)와 **OpenAI Codex**를 하나의 컨트롤 플레인 뒤에서
구동하며, 폴더·세션 목록에 두 에이전트가 함께 보이고 세션마다 에이전트별
동작을 제공합니다.

어느 컴퓨터의 어느 폴더든 원격 세션을 열고, 폴더별로 과거 세션을 조회 / 검색 /
이어가기 / 삭제하고, 커스텀 스킬을 동기화 — 전부 한 탭에서. 더 이상 각 머신에
SSH로 들어가 `claude --remote-control`을 손으로 칠 필요가 없습니다.

> **SSH로 닿는 모든 네트워크**에서 동작합니다 — LAN, VPN/메시(예: Tailscale),
> 공인 IP. **Tailscale 의존성은 없습니다**; 머신은 그냥 호스트 + SSH 사용자일
> 뿐입니다.

## 기능

- **멀티 머신** — 호스트 + SSH 사용자로 머신 등록 (Windows **및 Linux**); 각
  에이전트에 자동 재연결 SSH 터널로 접속.
- **멀티 에이전트** — Claude Code와 OpenAI Codex 세션을 나란히 (🟧 / 🟢 뱃지),
  통합된 "＋ 새 세션" 버튼 하나 — 누르면 에이전트 선택.
- **Claude 세션**은 `claude.ai/code` 탭(bridge URL)으로 열림; 이어가기 / 포크 /
  종료 / 삭제.
- **Codex 세션**은 **Codex app-server**를 통해 돌아가 **Codex 데스크톱 앱에도
  나타납니다**(아래 참고). 앱 내 **채팅 패널**이나 **인터랙티브 터미널**에서
  이어가고, 대화 미리보기, 삭제.
- **터미널 핸드오프** — **브라우저로 접속한 그 PC에서** codex TUI를 엽니다
  (컨트롤 플레인이 클라이언트의 tailnet IP로 어느 PC인지 판별; 원격 대상은
  `ssh -t`로 접속).
- **모델·노력(effort) 선택** — 새 세션 / 이어가기마다 둘 다 고르고, 헤더에서
  서버에 저장되는 기본값을 설정. 노력은 `--effort`로 전달되므로 그 머신의
  `effortLevel` 설정과 무관하게 고른 값으로 실행됩니다.
- **되감기 & 에이전트 전환** — 대화 기록의 임의 지점에서 새 세션으로 분기하거나,
  대화를 *다른* 에이전트로 이어가기 (🟧 Claude ↔ 🟢 Codex).
- **폴더별 탐색**(머신별); 제목 *및 대화 내용*으로 **세션 검색**; 이어가기 전
  **대화 미리보기**.
- **전체 검색** — 사이드바의 검색창 하나로. 입력하면 모든 머신의 폴더 목록이
  실시간으로 걸러지고, Enter를 누르면 **모든 머신의 모든 세션**을 찾습니다. 각
  에이전트가 자기 디스크를 병렬로 훑어 일치한 것만 돌려주며, 어디서 걸렸는지
  미리보기가 함께 표시됩니다.
- **자동 스킬 동기화** — `~/.claude/skills`가 모든 머신에서 각 스킬의 최신본으로
  수렴(mtime 기준 last-write-wins), 자격증명/암호화 파일은 제외. 아무 데서나
  수정하면 전파됩니다.
- **재부팅 후에도 유지** — 에이전트는 OS 서비스로 실행(Windows 작업 스케줄러 /
  Linux systemd / macOS launchd); 컨트롤 플레인 + 터널은 launchd로.
- **한/영 UI** — 🌐 토글; 설정은 서버에 저장됩니다.
- 단일 정적 웹 UI(바닐라 JS), 모바일 친화적. 백엔드는 순수 Python 3 표준
  라이브러리(Windows 에이전트는 `pywinpty` 추가).

### Codex 세션이 중복으로 보일 때

Codex 데스크톱 앱은 Claude Code 대화를 자기 기록으로 가져올 수 있습니다(외부
에이전트 가져오기). ASM은 두 저장소를 모두 읽으므로, 그대로 두면 같은 대화가 두
번 나열됩니다. Codex가 남기는 가져오기 기록
(`~/.codex/external_agent_session_imports.json`)을 기준으로 걸러냅니다. 다시
보려면 에이전트에 `CSM_SHOW_IMPORTED_CODEX=1`을 주면 됩니다. 애초에 복사가 생기지
않게 하려면 `~/.codex/config.toml`의 `[desktop]` 아래에
`external-agent-import-sync-enabled = false`를 넣으세요.

## Codex 데스크톱 앱 연동

ASM은 Codex 세션을 **Codex app-server**(`thread/start` + `turn/start`)로
생성합니다. 그래서 세션이 `source=vscode`로 기록되고 **Codex 데스크톱 앱의
프로젝트 목록에 나타납니다** — `codex exec`로 만든 세션은 `source=exec`라 앱이
숨깁니다. 새 세션은 앱이 다음 폴링에서 가져오므로 **재연결이 필요 없습니다**.

ASM이 **할 수 없는** 한 가지: **데스크톱 앱에 프로젝트/폴더를 추가하는 것.** 앱은
프로젝트 목록을 자체 내부(외부에서 쓸 수 없는 형식)에 보관합니다. 따라서:

1. **폴더를 Codex 데스크톱 앱에서 한 번 프로젝트로 추가**하세요.
2. 그러면 ASM이 그 폴더를 **`/codex/projects`**에 표시하고 거기에 세션을 만들며 —
   그 세션들이 앱의 해당 프로젝트 아래에 나타납니다.

요약하면: **프로젝트는 앱에서 추가(최초 1회, 수동), 세션은 ASM이 생성(무제한,
자동).** Codex에는 "앱에서 열기" 버튼이 없습니다(앱을 외부에서 조종할 수 없으므로)
— 하지만 ASM이 만든 세션은 그냥 앱에 나타납니다.

## 구조

```
브라우저 ──► 컨트롤 플레인(이 호스트) ──► SSH 터널 ──► 에이전트(각 머신)
             레지스트리 + UI + 인증 프록시              ~/.claude + ~/.codex
```

- **agent** (`csm/agent.py`) — 머신마다 하나. 그 머신의 `~/.claude` **및
  `~/.codex`**(폴더·세션·대화·스킬)를 읽고, `claude --remote-control` 세션을 실제
  PTY(POSIX `pty.fork`, Windows는 `pywinpty`의 ConPTY)로 띄우며, 머신 데스크톱에
  codex 터미널을 엽니다. `127.0.0.1`에 바인드; bearer 토큰(HMAC) 인증.
- **codex 어댑터** (`csm/codex.py`) — Codex rollout JSONL을 파싱하고, **codex
  app-server**(`thread/start` / `turn/start` / `thread/resume`)를 구동해 세션이
  앱에 보이게 하며, `config.toml [projects]`를 읽습니다.
- **컨트롤 플레인** (`csm/control.py`) — 웹 UI 서빙, 머신 레지스트리
  (`csm.config.json`) 보유, 브라우저 요청을 각 에이전트의 로컬 SSH 터널 포트로
  프록시(머신별 시크릿 첨부), 터미널 핸드오프를 클라이언트 IP로 알맞은 머신에
  라우팅.
- **tunnels** (`csm/tunnels.py`) — 머신마다 단방향 SSH 터널
  (`localPort → host:agentPort`)을 유지.
- **skill sync** (`csm/skillsync.py`) — 머신 간 스킬별 주기적 수렴.

## 요구사항

- **컨트롤 호스트**: Python 3.10+, OpenSSH 클라이언트, SSH 키.
- **각 머신**: Claude Code CLI 설치 + 로그인(`claude auth`), Python 3, OpenSSH
  서버, 그리고 컨트롤 호스트의 SSH 공개키가 등록돼 있어야 함. Codex 기능을 쓰려면:
  Codex CLI 설치 + 로그인(ChatGPT 로그인 또는 로컬 프로바이더) — 머신별 선택.
- Windows 에이전트는 `pywinpty`도 사용(배포 스크립트가 자동 설치).

## 빠른 시작

```bash
git clone <this-repo> && cd agent-session-manager
./run.sh                      # 로컬 에이전트 + 컨트롤 플레인 시작
# http://127.0.0.1:8765 열기
```

첫 실행 시 `csm.config.json`이 생성됩니다(랜덤 시크릿). 스키마는
`csm.config.example.json` 참고. 다른 기기에서 접속하려면 `csm.config.json`의
`control.host`를 VPN/LAN IP로 바인드하세요.

### 머신 추가

1. 대상 머신에 컨트롤 호스트의 SSH 키를 등록(최초 1회) — 컨트롤 플레인이 제공하는
   enroll 스크립트로:
   - **Windows** (관리자 PowerShell):
     ```powershell
     irm http://<control-host>:8765/enroll | iex
     ```
   - **Linux** (터미널; sudo 프롬프트):
     ```bash
     curl -fsSL http://<control-host>:8765/enroll?os=linux | bash
     ```
   (OpenSSH 서버 설정, 키 등록, Python 확인까지 처리).
2. 컨트롤 호스트에서 에이전트 배포:
   ```bash
   python scripts/deploy_agent.py <id> <host> <ssh-user>
   ```
   대상 OS는 자동 감지됩니다; 에이전트는 Windows 작업 스케줄러 또는 Linux systemd
   사용자 서비스로 자동 시작되고, 터널이 열리며, 머신이 `csm.config.json`에
   등록됩니다. UI를 새로고침하면 나타납니다. (UI의 **머신 추가** 다이얼로그도 동일
   동작.)

## 보안

- 에이전트는 `127.0.0.1`에 바인드되어 SSH 터널로만 접근되고, 각 에이전트는
  머신별 bearer 시크릿을 요구합니다. 웹 UI/시크릿은 컨트롤 호스트를 벗어나지
  않습니다. **컨트롤 플레인은 신뢰할 수 있는 네트워크(localhost/VPN/LAN)에
  두세요** — 에이전트는 임의 폴더에서 프로세스를 띄울 수 있으므로 셸 접근과 동등하게
  취급하세요.
- `csm.config.json`은 시크릿을 담으며 git-ignore됩니다. 절대 커밋하지 마세요.

## 변경 이력

[CHANGELOG.md](CHANGELOG.md) 참고. 현재 버전: **0.6.0**.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
