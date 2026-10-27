"""Browser login (OAuth + PKCE loopback), auto-refresh with rotation, and credential files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from promptli import AsyncPromptli, AuthenticationError
from promptli.auth import (
    OAuthCredential,
    OAuthToken,
    StaticToken,
    browser_login,
    credential_auth,
    save_credential,
)

BASE = "https://api.test"


def cred(**over: Any) -> OAuthCredential:
    c = OAuthCredential(client_id="mcl_1", refresh_token="r1", base_url=BASE, access_token="", expires_at=0)
    for k, v in over.items():
        setattr(c, k, v)
    return c


async def test_refresh_rotates_and_persists_before_use() -> None:
    calls: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        calls.append(form)
        n = len(calls)
        return httpx.Response(
            200, json={"access_token": f"a{n}", "refresh_token": f"r{n + 1}", "expires_in": 3600}
        )

    saved: list[str] = []
    tok = OAuthToken(
        cred(),
        save=lambda c: saved.append(c.refresh_token),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await tok.token() == "a1"
    assert calls[0] == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "mcl_1"}
    assert saved == ["r2"]  # the rotated refresh token is on disk before anything uses it
    assert await tok.token() == "a1" and len(calls) == 1  # still fresh: no network
    await tok.invalidate()
    assert await tok.token() == "a2" and calls[1]["refresh_token"] == "r2"


async def test_concurrent_requests_share_one_refresh() -> None:
    n = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": f"r{n + 1}", "expires_in": 3600}
        )

    tok = OAuthToken(cred(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await asyncio.gather(*(tok.token() for _ in range(5))) == ["a"] * 5
    assert n == 1  # a rotating refresh token must never be spent twice


async def test_revoked_login_raises_a_clear_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})

    tok = OAuthToken(cred(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthenticationError) as e:
        await tok.token()
    assert e.value.code == "login_expired"


async def test_requests_refresh_once_on_401_and_retry() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600}
            )
        seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer stale":
            return httpx.Response(
                401, json={"error": {"code": "api_key_revoked", "message": "revoked", "status": 401}}
            )
        return httpx.Response(200, json={"org": "org_1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tok = OAuthToken(cred(access_token="stale", expires_at=time.time() + 3000), http_client=http)
    async with AsyncPromptli(tok, base_url=BASE, http_client=http) as p:
        assert await p.transport.request("GET", "/v1/whoami") == {"org": "org_1"}
    assert seen == ["Bearer stale", "Bearer fresh"]


async def test_static_key_401_is_not_retried() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "invalid_api_key", "message": "bad", "status": 401}}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with AsyncPromptli("psk_test_x_y", base_url=BASE, http_client=http) as p:
        with pytest.raises(AuthenticationError):
            await p.transport.request("GET", "/v1/whoami")


def fake_server(state: dict[str, Any]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://console.test/oauth/authorize",
                    "token_endpoint": f"{BASE}/oauth/token",
                    "registration_endpoint": f"{BASE}/oauth/register",
                },
            )
        if path == "/oauth/register":
            body = json.loads(req.content)
            state["redirect_uris"] = body["redirect_uris"]
            state["client_name"] = body["client_name"]
            return httpx.Response(201, json={"client_id": "mcl_cli"})
        if path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            state["token_form"] = form
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if challenge != state["challenge"] or form["code"] != "the-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": "psk_test_acc", "refresh_token": "rt1", "expires_in": 3600}
            )
        if path == "/v1/whoami":
            return httpx.Response(200, json={"env": "test", "identity": {"id": "idn_1", "handle": "maya"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _browser(url: str, state: dict[str, Any], *, tamper_state: bool = False) -> None:
    """Plays the person approving in the browser: the console redirects to the CLI's loopback listener."""
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    state["challenge"] = q["code_challenge"]
    state["authorize_params"] = q
    async with httpx.AsyncClient() as http:
        r = await http.get(
            q["redirect_uri"], params={"code": "the-code", "state": "evil" if tamper_state else q["state"]}
        )
        state["landing"] = r.text


async def test_browser_login_end_to_end_with_loopback_and_pkce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    state: dict[str, Any] = {}
    urls: list[str] = []
    http = httpx.AsyncClient(transport=fake_server(state))
    task_holder: list[asyncio.Task[None]] = []

    def on_url(url: str) -> None:
        urls.append(url)
        task_holder.append(asyncio.ensure_future(_browser(url, state)))

    c = await browser_login(
        BASE,
        client_name="Claude Code on test-mac",
        on_url=on_url,
        login_hint="maya",
        want_number=True,
        env="test",
        http_client=http,
    )
    await task_holder[0]
    assert (c.client_id, c.refresh_token, c.access_token, c.identity, c.env) == (
        "mcl_cli",
        "rt1",
        "psk_test_acc",
        "maya",
        "test",
    )
    assert state["redirect_uris"][0].startswith("http://127.0.0.1:") and state["redirect_uris"][0].endswith(
        "/callback"
    )
    p = state["authorize_params"]
    assert (
        p["code_challenge_method"] == "S256"
        and p["login_hint"] == "maya"
        and p["promptli_number"] == "1"
        and p["promptli_env"] == "test"
    )
    assert state["token_form"]["grant_type"] == "authorization_code"
    assert "You're connected" in state["landing"]


async def test_browser_login_rejects_a_mismatched_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    state: dict[str, Any] = {}
    http = httpx.AsyncClient(transport=fake_server(state))
    holder: list[asyncio.Task[None]] = []

    with pytest.raises(AuthenticationError) as e:
        await browser_login(
            BASE,
            client_name="x",
            on_url=lambda u: holder.append(asyncio.ensure_future(_browser(u, state, tamper_state=True))),
            http_client=http,
        )
    await holder[0]
    assert e.value.code == "login_state_mismatch"
    assert "token_form" not in state  # never exchanged


async def test_no_browser_login_uses_the_console_code_page() -> None:
    state: dict[str, Any] = {}
    http = httpx.AsyncClient(transport=fake_server(state))

    def on_url(url: str) -> None:
        state["challenge"] = parse_qs(urlparse(url).query)["code_challenge"][0]

    async def paste() -> str:
        return " the-code \n"

    c = await browser_login(
        BASE, client_name="x", open_browser=False, on_url=on_url, paste_code=paste, http_client=http
    )
    assert state["redirect_uris"] == ["https://console.test/oauth/code"]
    assert c.refresh_token == "rt1"


async def test_credential_files_are_private_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "credentials"
    save_credential(
        path, cred(refresh_token="rt9", access_token="a", expires_at=time.time() + 3000, identity="maya")
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    src = credential_auth(path)
    assert isinstance(src, OAuthToken) and src.cred.identity == "maya" and await src.token() == "a"
    # A rotated refresh token is written back to the same file.
    src.cred.refresh_token = "rt10"
    src._save(src.cred)  # pyright: ignore[reportPrivateUsage, reportOptionalCall]
    assert json.loads(path.read_text())["oauth"]["refresh_token"] == "rt10"
    save_credential(path, {"api_key": "psk_test_k_s"})
    key_src = credential_auth(path)
    assert isinstance(key_src, StaticToken) and key_src.key == "psk_test_k_s"
    assert isinstance(credential_auth(path, env_key="psk_live_env"), StaticToken)
    assert credential_auth(tmp_path / "missing") is None


async def test_two_processes_sharing_a_login_never_spend_the_same_refresh_token(tmp_path: Path) -> None:
    """The gateway daemon and each MCP tool server share one credential file. The server only accepts the
    newest refresh token (rotation), so a process holding a stale one must pick up the new one from disk."""
    server = {"valid_refresh": "r1", "issued": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form["refresh_token"] != server["valid_refresh"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        server["issued"] += 1
        n = server["issued"]
        server["valid_refresh"] = f"r{n + 1}"
        return httpx.Response(
            200, json={"access_token": f"a{n}", "refresh_token": f"r{n + 1}", "expires_in": 3600}
        )

    path = tmp_path / "credentials"
    save_credential(path, cred())  # expired access token, refresh r1
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    daemon, tool = credential_auth(path), credential_auth(path)
    assert isinstance(daemon, OAuthToken) and isinstance(tool, OAuthToken)
    daemon._http = tool._http = http  # pyright: ignore[reportPrivateUsage]

    assert await daemon.token() == "a1"  # rotates r1 -> r2 and writes it to disk
    assert await tool.token() == "a1"  # still holds r1, but adopts the daemon's fresh token: no network
    assert server["issued"] == 1
    # The server refuses a1 for the tool (say it was rotated): it refreshes with the newest token from disk.
    await tool.invalidate()
    assert await tool.token() == "a2"
    assert server["issued"] == 2 and json.loads(path.read_text())["oauth"]["refresh_token"] == "r3"
    # And the daemon, now stale, adopts a2 from disk instead of failing with a spent refresh token.
    await daemon.invalidate()
    assert await daemon.token() == "a2"
    assert server["issued"] == 2  # no wasted refresh, no "login expired"
