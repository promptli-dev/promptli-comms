"""The contract every agent-runtime plugin implements (Claude Code, Codex, Hermes, OpenClaw, ...).

The gateway owns transport, sessions, delivery and setup; an adapter only knows how to run one turn
of its runtime and stream the result back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from promptli_gateway.tools import ToolSpec


class TurnKind(StrEnum):
    CHANNEL = "channel"  # an email / SMS arrived; the agent may reply
    VOICE = "voice"  # a live call utterance (M3): speak-first, short replies
    SUPERVISOR = "supervisor"  # steering a hosted call (M4)
    POST_CALL = "post_call"  # the call ended; do the follow-ups


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolActivity:
    name: str
    status: str  # started | completed | failed
    detail: str | None = None


@dataclass(frozen=True)
class TurnDone:
    text: str = ""  # full final text (the gateway also accumulates deltas)
    session_id: str | None = None  # runtime session id to persist for resume


@dataclass(frozen=True)
class TurnError:
    message: str
    retryable: bool = False


TurnChunk = TextDelta | ToolActivity | TurnDone | TurnError


@dataclass(frozen=True)
class AdapterCapabilities:
    streams_text: bool = False  # yields TextDelta before TurnDone (needed for fast voice)
    native_tools: bool = True  # the runtime calls our ToolSpecs itself
    resumable_sessions: bool = False  # session ids survive restarts
    interruptible: bool = False  # interrupt() stops a running turn (voice barge-in)
    voice: bool = True  # can answer live calls (Agent Link capability "voice")


@dataclass
class GatewayContext:
    identity_handle: str
    identity_email: str | None
    identity_number: str | None
    project_dir: str | None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


class RuntimeAdapter(Protocol):
    name: str
    capabilities: AdapterCapabilities

    async def start(self, ctx: GatewayContext, tools: list[ToolSpec]) -> None: ...

    def run_turn(
        self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None
    ) -> AsyncIterator[TurnChunk]: ...

    async def interrupt(self, session_key: str) -> None: ...

    async def reset_session(self, session_key: str) -> None: ...

    async def close(self) -> None: ...
