"""Agent Link client: an outbound WebSocket that streams an identity's events (spec/agent-link.md).

async with client.identity("glow").link(instance="laptop-1") as link:
    async for event in link:
        ...                      # handle
        await link.ack(event)    # persist progress (resume point after reconnects)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from promptli import __version__
from promptli.auth import StaticToken, TokenSource
from promptli.errors import AuthenticationError, PromptliError

log = logging.getLogger("promptli.link")

FATAL_CLOSE = {4001}  # auth/scope: reconnecting will not help
REPLACED = 4002


class AgentLink:
    def __init__(
        self,
        url: str,
        api_key: str | TokenSource,
        *,
        instance: str,
        identity: str | None = None,
        capabilities: list[str] | None = None,
        resume_after: int | None = None,
        on_state: Callable[[str], Awaitable[None] | None] | None = None,
        user_agent: str | None = None,
        reconnect: bool = True,
    ) -> None:
        params: list[str] = []
        if identity:
            params.append(f"identity={identity}")
        if capabilities:
            params.append("capabilities=" + ",".join(capabilities))
        self.url = url + (("?" + "&".join(params)) if params else "")
        self.auth: TokenSource = StaticToken(api_key) if isinstance(api_key, str) else api_key
        self.instance = instance
        self.resume_after = resume_after
        self.reconnect = reconnect
        self.on_state = on_state
        self.user_agent = f"promptli-python/{__version__}" + (f" {user_agent}" if user_agent else "")
        self.hello: dict[str, Any] = {}
        self.last_seq = 0
        self._ws: ClientConnection | None = None
        self._queue: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = asyncio.Event()

    # ---- lifecycle -----------------------------------------------------------------------
    async def __aenter__(self) -> AgentLink:
        await self.start()
        return self

    async def __aexit__(
        self, *exc: tuple[type[BaseException] | None, BaseException | None, TracebackType | None]
    ) -> None:
        await self.close()

    async def start(self, wait: bool = True, timeout: float = 15.0) -> None:  # noqa: ASYNC109
        self._task = asyncio.create_task(self._run())
        if wait:
            done, _ = await asyncio.wait(
                [asyncio.create_task(self._connected.wait()), self._task],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._task in done and self._task.exception():
                raise self._task.exception()  # type: ignore[misc]
            if not self._connected.is_set():
                raise PromptliError(
                    "Agent Link did not connect in time.", code="connection_error", retryable=True
                )

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ---- consumer API --------------------------------------------------------------------
    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def ack(self, event: dict[str, Any] | int) -> None:
        seq = event if isinstance(event, int) else int(event["seq"])
        self.last_seq = max(self.last_seq, seq)
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "ack", "seq": seq}))

    async def send(self, frame: dict[str, Any]) -> None:
        """Send a raw frame (call control frames in M3+)."""
        if self._ws is None:
            raise PromptliError("Agent Link is not connected.", code="connection_error", retryable=True)
        await self._ws.send(json.dumps(frame))

    # ---- internals -----------------------------------------------------------------------
    async def _state(self, state: str) -> None:
        if self.on_state:
            res = self.on_state(state)
            if asyncio.iscoroutine(res):
                await res

    async def _run(self) -> None:
        attempt = 0
        reauthed = False
        while not self._closed:
            try:
                try:
                    token = (
                        await self.auth.token()
                    )  # a browser login hands out a fresh token on each (re)connect
                except AuthenticationError:
                    raise
                except (
                    PromptliError,
                    OSError,
                ) as err:  # token endpoint unreachable: retry like a dropped link
                    raise ConnectionError(str(err)) from err
                async with websockets.connect(
                    self.url,
                    additional_headers={
                        "Authorization": f"Bearer {token}",
                        "X-Link-Instance": self.instance,
                        "User-Agent": self.user_agent,
                    },
                    open_timeout=15,
                    ping_interval=None,
                ) as ws:
                    self._ws = ws
                    hello = json.loads(await ws.recv())
                    if hello.get("type") == "error":
                        code = hello.get("code", "invalid_api_key")
                        if not reauthed and not isinstance(self.auth, StaticToken):
                            reauthed = True  # an access token that expired or rotated in flight: refresh once
                            await self.auth.invalidate()
                            continue
                        raise AuthenticationError(hello.get("message", code), code=code, status=401)
                    self.hello = hello
                    self.last_seq = max(self.last_seq, int(hello.get("cursor", 0)))
                    if self.resume_after is not None:
                        await ws.send(json.dumps({"type": "resume", "after": self.resume_after}))
                        self.resume_after = None
                    attempt = 0
                    reauthed = False
                    self._connected.set()
                    await self._state("connected")
                    async for raw in ws:
                        frame = json.loads(raw)
                        kind = frame.get("type")
                        if kind == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        elif kind == "event":
                            await self._queue.put(frame["event"])
                        elif kind == "error":
                            log.warning("agent link error: %s", frame)
                        else:
                            await self._queue.put(frame)  # call frames etc.; consumers ignore unknown types
            except AuthenticationError as err:
                await self._queue.put(err)
                return
            except websockets.ConnectionClosed as err:
                code = err.rcvd.code if err.rcvd else None
                if code in FATAL_CLOSE:
                    await self._queue.put(
                        AuthenticationError("Agent Link rejected the credential.", code="invalid_api_key")
                    )
                    return
                if code == REPLACED:
                    log.warning("agent link replaced by another connection with the same instance id")
            except (OSError, websockets.InvalidStatus, websockets.InvalidHandshake, TimeoutError) as err:
                log.debug("agent link connect failed: %s", err)
            finally:
                self._ws = None
                if self._connected.is_set():
                    self._connected.clear()
                    await self._state("disconnected")
            if self._closed or not self.reconnect:
                return
            attempt += 1
            await asyncio.sleep(min(30.0, 2 ** min(attempt, 5)) * (0.5 + random.random() / 2))  # noqa: S311
