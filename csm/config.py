"""Config loading/bootstrapping for csm.

The config file (csm.config.json at the repo root, or $CSM_CONFIG) holds the
shared bearer secret, the agent + control bind addresses, the path to claude,
and the machine registry. On first run we generate it with a random secret and
a single local machine pointing at the local agent.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "csm.config.json"


def config_path() -> Path:
    return Path(os.environ.get("CSM_CONFIG") or DEFAULT_PATH)


def load_config() -> dict:
    p = config_path()
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    else:
        cfg = _bootstrap()
        p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        os.chmod(p, 0o600)
    return cfg


def _bootstrap() -> dict:
    host = socket.gethostname().split(".")[0]
    return {
        "secret": secrets.token_hex(32),
        "claudePath": shutil.which("claude") or str(Path.home() / ".local/bin/claude"),
        "selfId": "local",
        "selfName": host,
        "agent": {"host": "127.0.0.1", "port": 8766},
        "control": {"host": "127.0.0.1", "port": 8765},
        # Registered remote machines. Each: {id, name(nickname), host, sshUser,
        # sshPort, os, agentPort, localPort, secret, pythonExe, claudePath}.
        # Added via the web UI ("+ 머신") or scripts/deploy_agent.py.
        "machines": [],
    }
