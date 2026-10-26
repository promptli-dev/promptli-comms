"""OpenAI Codex CLI: `codex exec --json` per turn, resumed by thread id; Promptli tools via the local MCP server.

Events (JSONL): thread.started {thread_id}, item.started/completed {item: {type: agent_message | mcp_tool_call |
command_execution | reasoning, ...}}, turn.completed, error. Codex sends whole messages, not token deltas,
so it is a fit for email and texts (not live voice).
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from typing import Any

from promptli_gateway.adapter import AdapterCapabilities, TextDelta, ToolActivity, TurnChunk, TurnDone, TurnError, TurnKind
from promptli_gateway.adapters.cli_base import CliAdapter, as_dict


class CodexAdapter(CliAdapter):
    name = "codex"
    binary = "codex"
    capabilities = AdapterCapabilities(streams_text=False, interruptible=True, voice=False, native_tools=True, resumable_sessions=True)

    def __init__(self, *, model: str | None = None, sandbox: str = "read-only", **kw: Any) -> None:
        super().__init__(**kw)
        self.model = model
        self.sandbox = sandbox  # read-only | workspace-write | danger-full-access (the owner's choice)

    def mcp_args(self) -> list[str]:
        """Expose Promptli's tools (email, texts, contacts, ...) to Codex as an MCP server for this turn."""
        args = json.dumps(["-m", "promptli_gateway.mcp_stdio", "--runtime", "codex"])
        return ["-c", f"mcp_servers.promptli.command={json.dumps(sys.executable)}", "-c", f"mcp_servers.promptli.args={args}"]

    def command(self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None) -> list[str]:
        text = prompt if resume_id else self.preamble() + prompt
        base = [self.bin, "exec"]
        if resume_id:
            base += ["resume", resume_id]
        opts = ["--json", "--skip-git-repo-check", *self.mcp_args()]
        if not resume_id:  # resume keeps the session's sandbox
            opts += ["--sandbox", self.sandbox]
        if self.model:
            opts += ["-m", self.model]
        return [*base, *opts, *self.extra_args, text]

    async def parse(self, lines: AsyncIterator[str], session_key: str) -> AsyncIterator[TurnChunk]:
        thread: str | None = None
        last = ""
        async for line in lines:
            try:
                ev: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            item = as_dict(ev.get("item"))
            if kind == "thread.started":
                thread = str(ev.get("thread_id") or "") or None
            elif kind == "item.started" and item.get("type") in ("mcp_tool_call", "command_execution"):
                yield ToolActivity(str(item.get("tool") or item.get("command") or item.get("type")), "started")
            elif kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    yield TextDelta(("\n\n" if last else "") + text)
                    last = text
            elif kind == "turn.completed":
                yield TurnDone(text=last, session_id=thread)
                return
            elif kind in ("error", "turn.failed"):
                msg = str(ev.get("message") or as_dict(ev.get("error")).get("message") or "codex error")
                yield TurnError(msg, retryable=True)
                return
