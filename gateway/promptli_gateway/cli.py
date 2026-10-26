"""`promptli-agent`: connect any supported runtime (Codex, Hermes, OpenClaw, a custom HTTP agent) to Promptli
Comms: email, texts and calls arrive as turns; replies go back on the same channel.

    promptli-agent setup --runtime codex --identity my-agent     # key from PROMPTLI_API_KEY or prompted
    promptli-agent run --runtime codex                            # foreground
    promptli-agent start --runtime codex                          # background service (launchd / systemd)
    promptli-agent doctor --runtime codex
    promptli-agent mcp --runtime codex                            # Promptli tools as a stdio MCP server
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from promptli_gateway.adapter import RuntimeAdapter
from promptli_gateway.config import GatewayConfig
from promptli_gateway.setup.autostart import autostart_status, install_autostart, uninstall_autostart
from promptli_gateway.setup.bootstrap import bootstrap
from promptli_gateway.setup.doctor import doctor

RUNTIMES = ("codex", "hermes", "openclaw", "generic-http")
WARNING = (
    "Anyone who knows your agent's email address or phone number can reach it, and each message becomes a turn "
    "in your runtime with ITS permissions in the project directory. Keep the runtime's tool permissions tight "
    "(e.g. Codex --sandbox read-only, the default here): a stranger's email can try to talk the agent into using "
    "any tool it is allowed to use (prompt injection)."
)
TOOL_HINTS = {
    "codex": "Codex gets the Promptli tools automatically for each turn (local MCP server).",
    "hermes": "Give Hermes the Promptli tools:  hermes mcp add promptli -- {py} -m promptli_gateway.mcp_stdio --runtime hermes",
    "openclaw": "Give OpenClaw the Promptli tools: add an MCP server running `{py} -m promptli_gateway.mcp_stdio --runtime openclaw`.",
    "generic-http": "Your HTTP agent receives turns at its /turn endpoint; it can call the Promptli API itself.",
}


def make_adapter(runtime: str, extra: dict[str, Any]) -> RuntimeAdapter:
    if runtime == "codex":
        from promptli_gateway.adapters.codex import CodexAdapter

        return CodexAdapter(model=extra.get("model"), sandbox=str(extra.get("sandbox") or "read-only"))
    if runtime == "hermes":
        from promptli_gateway.adapters.hermes import HermesAdapter

        return HermesAdapter(model=extra.get("model"), provider=extra.get("provider"))
    if runtime == "openclaw":
        from promptli_gateway.adapters.openclaw import OpenClawAdapter

        return OpenClawAdapter(agent=extra.get("agent"), model=extra.get("model"), local=bool(extra.get("local")))
    if runtime == "generic-http":
        from promptli_gateway.adapters.generic_http import GenericHttpAdapter

        url = extra.get("url")
        if not url:
            raise SystemExit("generic-http needs --url (your agent's /turn endpoint).")
        return GenericHttpAdapter(str(url), token=os.environ.get("PROMPTLI_AGENT_TOKEN"))
    raise SystemExit(f"Unknown runtime {runtime!r}; choose one of {', '.join(RUNTIMES)}.")


def _extra(a: argparse.Namespace) -> dict[str, Any]:
    keys = ("model", "sandbox", "provider", "agent", "local", "url")
    return {k: getattr(a, k) for k in keys if getattr(a, k, None) not in (None, False)}


def cmd_setup(a: argparse.Namespace) -> int:
    print(f"Promptli Comms for {a.runtime}\n\n{WARNING}\n")
    if not a.yes and input("I understand. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    binary = {"generic-http": None}.get(a.runtime, a.runtime)
    if binary and not shutil.which(binary):
        print(f"warning: `{binary}` is not on PATH yet; install it before `promptli-agent run`.")
    key = os.environ.get("PROMPTLI_API_KEY") or getpass.getpass("Promptli API key (input hidden): ").strip()
    res = asyncio.run(
        bootstrap(
            runtime=a.runtime,
            api_key=key,
            identity=a.identity,
            base_url=a.base_url,
            project_dir=str(Path(a.project_dir or os.getcwd()).resolve()),
            want_number=a.number,
            phone_region=a.region,
            extra=_extra(a),
        )
    )
    print(json.dumps(res.__dict__, indent=2, default=str))
    if res.status == "error":
        return 1
    print("\n" + TOOL_HINTS[a.runtime].format(py=sys.executable))
    print(f"Next: promptli-agent start --runtime {a.runtime}   (or `run` in the foreground)")
    return 0


def _config(runtime: str) -> GatewayConfig:
    cfg = GatewayConfig.load(runtime)
    if cfg is None:
        raise SystemExit(f"Not configured: run `promptli-agent setup --runtime {runtime}` first.")
    return cfg


def cmd_run(a: argparse.Namespace) -> int:
    from promptli_gateway.core import Gateway

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    cfg = _config(a.runtime)
    adapter = make_adapter(a.runtime, {**cfg.extra, **_extra(a)})

    async def main_loop() -> None:
        gw = Gateway(cfg, adapter)
        await gw.run()

    asyncio.run(main_loop())
    return 0


def cmd_start(a: argparse.Namespace) -> int:
    _config(a.runtime)
    path = install_autostart(a.runtime, [sys.executable, "-m", "promptli_gateway.cli", "run", "--runtime", a.runtime])
    print(f"Started (runs at login): {path}")
    return 0


def cmd_stop(a: argparse.Namespace) -> int:
    print("Stopped." if uninstall_autostart(a.runtime) else "Service was not installed.")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    ok, detail = autostart_status(a.runtime)
    print(f"service: {detail}")
    return 0 if ok else 1


def cmd_doctor(a: argparse.Namespace) -> int:
    async def runtime_check() -> tuple[bool, str]:
        binary = {"generic-http": None}.get(a.runtime, a.runtime)
        if not binary:
            return True, "custom HTTP agent"
        path = shutil.which(binary)
        return (path is not None), (path or f"`{binary}` not found on PATH")

    rep = asyncio.run(doctor(a.runtime, runtime_check=runtime_check))
    for check in rep.checks:
        print(f"{'ok ' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    return 0 if rep.ok else 1


def cmd_mcp(a: argparse.Namespace) -> int:
    from promptli_gateway.mcp_stdio import serve

    asyncio.run(serve(a.runtime))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="promptli-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--runtime", required=True, choices=RUNTIMES)
        p.set_defaults(fn=fn)
        return p

    s = add("setup", cmd_setup, "connect a key, create/attach the agent, save config")
    s.add_argument("--identity", help="agent handle (existing or new)")
    s.add_argument("--project-dir", help="directory the runtime works in (default: here)")
    s.add_argument("--number", action="store_true", help="also get a phone number")
    s.add_argument("--region", help="phone number area code / region")
    s.add_argument("--base-url", default=os.environ.get("PROMPTLI_BASE_URL", "https://api.comms.promptli.com"))
    s.add_argument("--yes", "-y", action="store_true", help="accept the security warning")
    for p in (s, r := add("run", cmd_run, "run the gateway in the foreground")):
        p.add_argument("--model")
        p.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], help="codex")
        p.add_argument("--provider", help="hermes")
        p.add_argument("--agent", help="openclaw agent id")
        p.add_argument("--local", action="store_true", help="openclaw: embedded agent")
        p.add_argument("--url", help="generic-http: your agent's /turn URL")
    r.add_argument("-v", "--verbose", action="store_true")
    add("start", cmd_start, "install and start the background service")
    add("stop", cmd_stop, "stop and remove the background service")
    add("status", cmd_status, "background service status")
    add("doctor", cmd_doctor, "check config, key, channels and the runtime")
    add("mcp", cmd_mcp, "serve Promptli tools over stdio MCP")
    a = ap.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
