"""Expose the gateway's ToolSpecs as a local MCP server over stdio (for Codex, OpenCode and any MCP host).

python -m promptli_gateway.mcp_stdio --runtime codex
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

from promptli import AsyncPromptli
from promptli_gateway.config import GatewayConfig
from promptli_gateway.tools import ToolSpec
from promptli_gateway.tools.comms import comms_tools


def build_server(tools: list[ToolSpec], name: str = "promptli") -> Server:
    by_name = {t.name: t for t in tools}

    async def on_list_tools(_ctx: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(
            tools=[Tool(name=t.name, description=t.description, input_schema=t.input_schema) for t in tools]
        )

    async def on_call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        spec = by_name.get(params.name)
        if spec is None:
            payload: dict[str, Any] = {"ok": False, "error": "unknown_tool"}
        else:
            payload = await spec.handler(dict(params.arguments or {}))
        text = json.dumps(payload, default=str)
        is_error = not payload.get("ok", True)
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)

    return Server(name, version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve(runtime: str) -> None:
    cfg = GatewayConfig.load(runtime)
    if cfg is None:
        raise SystemExit(f"Not configured: run `{runtime} bootstrap` first.")
    async with AsyncPromptli(cfg.auth(), base_url=cfg.base_url, user_agent=f"promptli-mcp {runtime}") as p:
        server = build_server(comms_tools(p, cfg.identity, lambda _c, _r: None))
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True)
    asyncio.run(serve(ap.parse_args().runtime))


if __name__ == "__main__":
    main()
