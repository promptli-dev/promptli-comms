"""Credentials: API keys, and browser login (OAuth 2.1 + PKCE, RFC 8252 loopback) with auto-refresh.

```python
cred = await browser_login("https://api.comms.promptli.com", client_name="My tool on laptop")
save_credential(path, cred)                  # {"oauth": {...}}, mode 600
async with AsyncPromptli(credential_auth(path)) as p:   # refreshes itself and persists rotated tokens
    ...
```

Access tokens are short-lived (1 hour) identity-scoped keys; refresh tokens rotate on every use, so the
newest one is written back to disk before it is used again.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from promptli.errors import AuthenticationError, PromptliError

REFRESH_SKEW_S = 60  # refresh this long before the access token expires
LOGIN_TIMEOUT_S = 300


@runtime_checkable
class TokenSource(Protocol):
    """Where a client gets the bearer token for each request and connection."""

    async def token(self) -> str: ...

    async def invalidate(self) -> None:
        """The server rejected the current token: get a fresh one next time (no-op for static keys)."""
        ...


class StaticToken:
    """A plain API key (psk_live_… / psk_test_…)."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def token(self) -> str:
        return self.key

    async def invalidate(self) -> None:
        return None


@dataclass
class OAuthCredential:
    client_id: str
    refresh_token: str
    base_url: str
    access_token: str = ""
    expires_at: float = 0.0
    identity: str | None = None  # agent handle the login is bound to
    env: str | None = None  # live | test

    def to_dict(self) -> dict[str, Any]:
        return {"oauth": asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthCredential | None:
        raw = data.get("oauth")
        if not isinstance(raw, dict):
            return None
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


Saver = Callable[[OAuthCredential], None]
Loader = Callable[[], "OAuthCredential | None"]


@contextlib.contextmanager
def _file_lock(path: Path | None) -> Generator[None]:
    """Exclusive lock shared by every process using one credential file (a daemon and its tool servers)."""
    if path is None:
        yield
        return
    try:
        import fcntl  # POSIX only; Windows runs without cross-process locking
    except ImportError:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class OAuthToken:
    """Refreshes the access token before it expires, under a lock, and persists every rotated refresh token
    (via `save`) before using it, so a crash never strands the only valid refresh token in memory."""

    def __init__(
        self,
        cred: OAuthCredential,
        save: Saver | None = None,
        *,
        reload: Loader | None = None,
        lock_path: Path | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cred = cred
        self._save = save
        self._reload = reload
        self._lock_path = lock_path
        self._http = http_client
        self._lock = asyncio.Lock()
        self._rejected = ""  # an access token the server refused

    async def token(self) -> str:
        async with self._lock:
            if self.cred.access_token and time.time() < self.cred.expires_at - REFRESH_SKEW_S:
                return self.cred.access_token
            await self._refresh()
            return self.cred.access_token

    async def invalidate(self) -> None:
        self._rejected = self.cred.access_token
        self.cred.expires_at = 0.0

    async def _refresh(self) -> None:
        # Several processes may share this login (rotating refresh tokens). Serialize refreshes across
        # them and re-read the file first: another process may already hold a fresh token, and our
        # refresh token may already be spent.
        await asyncio.to_thread(self._acquire)
        try:
            if self._reload is not None:
                disk = self._reload()
                if disk is not None and disk.refresh_token != self.cred.refresh_token:
                    self.cred = disk
                    fresh = disk.access_token and time.time() < disk.expires_at - REFRESH_SKEW_S
                    if fresh and disk.access_token != self._rejected:
                        return
            await self._refresh_network()
        finally:
            self._release()

    def _acquire(self) -> None:
        self._held = _file_lock(self._lock_path)
        self._held.__enter__()

    def _release(self) -> None:
        held = getattr(self, "_held", None)
        if held is not None:
            held.__exit__(None, None, None)
            self._held = None

    async def _refresh_network(self) -> None:
        try:
            await self._refresh_once()
        except httpx.HTTPError as e:
            raise PromptliError(
                f"Couldn't reach Promptli to refresh the login: {e}", code="connection_error", retryable=True
            ) from e

    async def _refresh_once(self) -> None:
        async with _client(self._http) as http:
            res = await http.post(
                self.cred.base_url.rstrip("/") + "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.cred.refresh_token,
                    "client_id": self.cred.client_id,
                },
            )
        body = _json(res)
        if res.status_code >= 400:
            if body.get("error") == "invalid_grant":
                raise AuthenticationError(
                    "Your browser login was revoked or has expired. Log in again.",
                    code="login_expired",
                    status=401,
                )
            raise PromptliError(
                str(body.get("error_description") or f"Token refresh failed ({res.status_code})."),
                code=str(body.get("error") or "token_refresh_failed"),
                status=res.status_code,
                retryable=res.status_code >= 500,
            )
        self.cred.refresh_token = str(body["refresh_token"])
        self.cred.access_token = str(body["access_token"])
        self.cred.expires_at = time.time() + float(body.get("expires_in") or 3600)
        if self._save:
            self._save(self.cred)

    async def revoke(self) -> None:
        """Log out: the server revokes the login (and with it the access token)."""
        async with _client(self._http) as http:
            with contextlib.suppress(httpx.HTTPError):
                await http.post(
                    self.cred.base_url.rstrip("/") + "/oauth/revoke", data={"token": self.cred.refresh_token}
                )


# ---- files ------------------------------------------------------------------------------------------


def save_credential(path: Path, data: OAuthCredential | dict[str, Any]) -> None:
    """Atomically write a credential file with mode 600 (no window where it is world-readable)."""
    payload = data.to_dict() if isinstance(data, OAuthCredential) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def credential_auth(path: Path, *, env_key: str | None = None) -> TokenSource | None:
    """The token source stored in a credential file: an API key, or a browser login that refreshes itself
    and writes rotated tokens back to the same file. `env_key` (e.g. $PROMPTLI_API_KEY) wins if set."""
    if env_key:
        return StaticToken(env_key)
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    cred = OAuthCredential.from_dict(data)
    if cred is not None:

        def reload() -> OAuthCredential | None:
            try:
                return OAuthCredential.from_dict(json.loads(path.read_text()))
            except (OSError, ValueError):
                return None

        return OAuthToken(
            cred,
            save=lambda c: save_credential(path, {**data, **c.to_dict()}),
            reload=reload,
            lock_path=path.with_name(path.name + ".lock"),
        )
    key = data.get("api_key")
    return StaticToken(str(key)) if key else None


# ---- browser login ------------------------------------------------------------------------------------

_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Promptli</title>"
    "<body style='font:16px system-ui;margin:15vh auto;max-width:28em;text-align:center'>"
    "<h2>{title}</h2><p>{msg}</p></body>"
)


async def _wait_for_callback(
    server_ready: asyncio.Future[int], result: asyncio.Future[dict[str, str]]
) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            url = urlparse(target)
            if url.path != "/callback":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            ok = "code" in q
            page = _DONE_PAGE.format(
                title="You're connected" if ok else "Login cancelled",
                msg="You can close this tab and go back to your terminal."
                if ok
                else "Nothing was connected. You can close this tab.",
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                + f"Content-Length: {len(page)}\r\n\r\n".encode()
                + page
            )
            if not result.done():
                result.set_result(q)
        except (TimeoutError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await writer.drain()
                writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    server_ready.set_result(server.sockets[0].getsockname()[1])
    async with server:
        with contextlib.suppress(asyncio.CancelledError):
            await result
    server.close()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def browser_login(
    base_url: str,
    *,
    client_name: str,
    open_browser: bool = True,
    login_hint: str | None = None,
    want_number: bool = False,
    env: str | None = None,
    on_url: Callable[[str], None] | None = None,
    paste_code: Callable[[], Awaitable[str]] | None = None,
    timeout: float = LOGIN_TIMEOUT_S,  # noqa: ASYNC109 - bounds the whole interactive login, not one call
    http_client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Log in with the browser and return a credential bound to the agent the person picked.

    With `open_browser=False` (SSH, headless) the console shows a one-time code after approval and
    `paste_code` must return it. `on_url` is always called with the login URL so it can be printed.
    """
    base = base_url.rstrip("/")
    async with _client(http_client) as http:
        meta = _json(await http.get(base + "/.well-known/oauth-authorization-server"))
        authorize_ep, token_ep = str(meta["authorization_endpoint"]), str(meta["token_endpoint"])

        result: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        listener: asyncio.Task[None] | None = None
        if open_browser:
            ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            listener = asyncio.create_task(_wait_for_callback(ready, result))
            redirect_uri = f"http://127.0.0.1:{await ready}/callback"
        else:
            if paste_code is None:
                raise ValueError("paste_code is required when open_browser is False")
            console = urlparse(authorize_ep)
            redirect_uri = f"{console.scheme}://{console.netloc}/oauth/code"
        try:
            reg = await http.post(
                str(meta["registration_endpoint"]),
                json={"client_name": client_name, "redirect_uris": [redirect_uri]},
            )
            if reg.status_code >= 400:
                raise PromptliError(
                    str(_json(reg).get("error_description") or "Could not register the login client."),
                    code="login_failed",
                    status=reg.status_code,
                )
            client_id = str(_json(reg)["client_id"])
            verifier, challenge = _pkce()
            state = secrets.token_urlsafe(24)
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "scope": "promptli",
            }
            if login_hint:
                params["login_hint"] = login_hint
            if want_number:
                params["promptli_number"] = "1"
            if env in ("live", "test"):
                params["promptli_env"] = env
            url = f"{authorize_ep}?{urlencode(params)}"
            if on_url:
                on_url(url)
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(url)
                try:
                    q = await asyncio.wait_for(asyncio.shield(result), timeout)
                except TimeoutError as e:
                    raise PromptliError(
                        "Timed out waiting for the browser.", code="login_timeout", status=408
                    ) from e
                if q.get("error"):
                    raise AuthenticationError("The login was not approved.", code="login_denied", status=401)
                if not secrets.compare_digest(q.get("state", ""), state):
                    raise AuthenticationError(
                        "The login response didn't match this request.",
                        code="login_state_mismatch",
                        status=401,
                    )
                code = q["code"]
            else:
                code = (await asyncio.wait_for(paste_code(), timeout)).strip()  # pyright: ignore[reportOptionalCall]
        finally:
            if listener is not None:
                if not result.done():
                    result.cancel()
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await listener

        tok = await http.post(
            token_ep,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        body = _json(tok)
        if tok.status_code >= 400:
            raise AuthenticationError(
                str(body.get("error_description") or "The login code was invalid or expired."),
                code="login_failed",
                status=tok.status_code,
            )
        cred = OAuthCredential(
            client_id=client_id,
            refresh_token=str(body["refresh_token"]),
            base_url=base,
            access_token=str(body["access_token"]),
            expires_at=time.time() + float(body.get("expires_in") or 3600),
        )
        who = _json(
            await http.get(base + "/v1/whoami", headers={"Authorization": f"Bearer {cred.access_token}"})
        )
        ident: Any = who.get("identity")
        handle: Any = cast("dict[str, Any]", ident).get("handle") if isinstance(ident, dict) else None
        cred.identity = str(handle) if handle else None
        cred.env = str(who.get("env")) if who.get("env") else None
        return cred


# ---- helpers ------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _client(given: httpx.AsyncClient | None) -> AsyncGenerator[httpx.AsyncClient]:
    if given is not None:
        yield given
        return
    async with httpx.AsyncClient(timeout=30) as http:
        yield http


def _json(res: httpx.Response) -> dict[str, Any]:
    try:
        data: Any = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]
