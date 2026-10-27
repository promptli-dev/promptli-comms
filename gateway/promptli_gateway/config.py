"""Gateway configuration, stored per runtime in ~/.promptli/<runtime>/.

Files: config.json and credentials (mode 600).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from promptli.auth import OAuthCredential, TokenSource, credential_auth, save_credential

DEFAULT_BASE_URL = "https://api.comms.promptli.com"


def home_dir(runtime: str) -> Path:
    root = Path(os.environ.get("PROMPTLI_HOME", Path.home() / ".promptli"))
    return root / runtime


@dataclass
class GatewayConfig:
    runtime: str
    identity: str  # handle
    base_url: str = DEFAULT_BASE_URL
    instance_id: str = field(
        default_factory=lambda: f"{socket.gethostname().split('.')[0]}-{secrets.token_hex(4)}"
    )
    project_dir: str | None = None
    auto_reply: bool = True  # send the agent's final text back on the same channel when no tool replied
    batch_window_s: float = 1.5  # coalesce bursts of inbound messages from one person into one turn
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def dir(self) -> Path:
        return home_dir(self.runtime)

    @property
    def state_path(self) -> Path:
        return self.dir / "state.db"

    @property
    def credentials_path(self) -> Path:
        return self.dir / "credentials"

    def save(self, api_key: str | None = None, oauth: OAuthCredential | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "config.json").write_text(json.dumps(asdict(self), indent=2))
        if oauth is not None:
            save_credential(self.credentials_path, oauth)
        elif api_key:
            save_credential(self.credentials_path, {"api_key": api_key})

    def auth(self) -> TokenSource:
        """The credential to use: $PROMPTLI_API_KEY, else the stored API key or browser login (which refreshes
        itself and shares rotated tokens with every process using this config)."""
        src = credential_auth(self.credentials_path, env_key=os.environ.get("PROMPTLI_API_KEY"))
        if src is None:
            raise RuntimeError(
                f"Not logged in: run setup, or set PROMPTLI_API_KEY ({self.credentials_path} missing)"
            )
        return src

    def credential_kind(self) -> str:
        """'api_key', 'browser_login' or 'none' (for doctor/status)."""
        if os.environ.get("PROMPTLI_API_KEY"):
            return "api_key"
        if not self.credentials_path.exists():
            return "none"
        data: dict[str, Any] = json.loads(self.credentials_path.read_text())
        return "browser_login" if "oauth" in data else "api_key"

    @classmethod
    def load(cls, runtime: str) -> GatewayConfig | None:
        p = home_dir(runtime) / "config.json"
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text()))

    def api_key(self) -> str:
        env = os.environ.get("PROMPTLI_API_KEY")
        if env:
            return env
        cred = self.dir / "credentials"
        if not cred.exists():
            raise RuntimeError(f"No API key: run setup, or set PROMPTLI_API_KEY ({cred} missing)")
        data: dict[str, Any] = json.loads(cred.read_text())
        if "api_key" not in data:
            raise RuntimeError("This setup uses a browser login, not an API key (use auth()).")
        return str(data["api_key"])
