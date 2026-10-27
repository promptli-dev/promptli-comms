"""The gateway stores either an API key or a browser login, and bootstrap accepts a browser login as-is."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from promptli import OAuthCredential, OAuthToken, StaticToken
from promptli_gateway.config import GatewayConfig
from promptli_gateway.setup import bootstrap as boot


def login(**over: Any) -> OAuthCredential:
    c = OAuthCredential(
        client_id="mcl_1",
        refresh_token="r1",
        base_url="https://api.test",
        access_token="psk_test_acc",
        expires_at=time.time() + 3000,
        identity="maya",
        env="test",
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_config_round_trips_both_credential_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    monkeypatch.delenv("PROMPTLI_API_KEY", raising=False)
    cfg = GatewayConfig(runtime="claude-code", identity="maya")
    assert cfg.credential_kind() == "none"
    with pytest.raises(RuntimeError, match="Not logged in"):
        cfg.auth()

    cfg.save(api_key="psk_test_k_s")
    assert cfg.credential_kind() == "api_key" and cfg.api_key() == "psk_test_k_s"
    assert isinstance(cfg.auth(), StaticToken)

    cfg.save(oauth=login())
    assert stat.S_IMODE(cfg.credentials_path.stat().st_mode) == 0o600
    assert cfg.credential_kind() == "browser_login"
    src = cfg.auth()
    assert isinstance(src, OAuthToken) and src.cred.identity == "maya"
    with pytest.raises(RuntimeError):
        cfg.api_key()  # a browser login has no long-lived key to hand out

    monkeypatch.setenv("PROMPTLI_API_KEY", "psk_test_env_k")
    assert cfg.credential_kind() == "api_key" and isinstance(cfg.auth(), StaticToken)


class FakePromptli:
    """Stands in for AsyncPromptli: records the token source and answers the three bootstrap reads."""

    seen: ClassVar[list[Any]] = []

    def __init__(self, auth: Any, **_: Any) -> None:
        FakePromptli.seen.append(auth)

    async def __aenter__(self) -> FakePromptli:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def whoami(self) -> Any:
        return SimpleNamespace(env="test", key_kind="oauth", identity=SimpleNamespace(handle="maya"))

    def identity(self, handle: str) -> Any:
        async def get() -> Any:
            return SimpleNamespace(email_address=f"{handle}@agents.promptli.com")

        async def channel_status() -> Any:
            return SimpleNamespace(sms=SimpleNamespace(number="+14155550100"))

        return SimpleNamespace(get=get, channel_status=channel_status)


async def test_bootstrap_with_a_browser_login_mints_nothing_and_saves_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    monkeypatch.delenv("PROMPTLI_API_KEY", raising=False)
    monkeypatch.setattr(boot, "AsyncPromptli", FakePromptli)
    FakePromptli.seen = []

    res = await boot.bootstrap(runtime="claude-code", credential=login(), project_dir="/work")
    assert res.status == "ok", res.error
    assert (res.identity, res.env, res.key_kind) == ("maya", "test", "oauth")
    assert (res.email_address, res.phone_number) == ("maya@agents.promptli.com", "+14155550100")
    assert isinstance(FakePromptli.seen[0], OAuthToken)  # never an admin key, never a minted key

    cfg = GatewayConfig.load("claude-code")
    assert cfg is not None and cfg.identity == "maya" and cfg.base_url == "https://api.test"
    assert cfg.project_dir == "/work"
    stored = json.loads(cfg.credentials_path.read_text())
    assert set(stored) == {"oauth"} and stored["oauth"]["refresh_token"] == "r1"


async def test_bootstrap_needs_some_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    res = await boot.bootstrap(runtime="claude-code")
    assert res.status == "error" and res.error and res.error["code"] == "credential_required"
