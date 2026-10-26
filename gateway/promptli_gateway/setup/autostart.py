"""Run the gateway at login: macOS launchd (LaunchAgent) or Linux systemd --user."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from promptli_gateway.config import home_dir


def label(runtime: str) -> str:
    return f"ai.promptli.{runtime}"


def plist_path(runtime: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label(runtime)}.plist"


def unit_path(runtime: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / f"{label(runtime)}.service"


def systemd_unit(runtime: str, program: list[str], env: dict[str, str]) -> str:
    def q(a: str) -> str:
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in a for c in ' "\\') else a

    lines = [
        "[Unit]",
        f"Description=Promptli Comms gateway ({runtime})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"ExecStart={' '.join(q(a) for a in program)}",
        "Restart=on-failure",
        "RestartSec=10",
        *[f"Environment={q(f'{k}={v}')}" for k, v in env.items()],
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _install_systemd(runtime: str, program: list[str], env: dict[str, str]) -> str:
    p = unit_path(runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(systemd_unit(runtime, program, env))
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)  # noqa: S603, S607
    subprocess.run(["systemctl", "--user", "enable", "--now", p.name], capture_output=True, check=True)  # noqa: S603, S607
    return str(p)


def install_autostart(runtime: str, program: list[str], *, env: dict[str, str] | None = None) -> str:
    """Keep `program` running at login (LaunchAgent on macOS, systemd --user on Linux). Returns its path."""
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})}
    if sys.platform.startswith("linux"):
        return _install_systemd(runtime, program, full_env)
    if sys.platform != "darwin":
        raise RuntimeError("Autostart supports macOS (launchd) and Linux (systemd --user).")
    logs = home_dir(runtime) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": label(runtime),
        "ProgramArguments": program,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "gateway.log"),
        "StandardErrorPath": str(logs / "gateway.err.log"),
        "EnvironmentVariables": full_env,
        "ProcessType": "Background",
    }
    p = plist_path(runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        plistlib.dump(data, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(p)], capture_output=True, check=False)  # noqa: S603, S607
    subprocess.run(["launchctl", "bootstrap", domain, str(p)], capture_output=True, check=True)  # noqa: S603, S607
    return str(p)


def uninstall_autostart(runtime: str) -> bool:
    if sys.platform.startswith("linux"):
        u = unit_path(runtime)
        if not u.exists():
            return False
        subprocess.run(["systemctl", "--user", "disable", "--now", u.name], capture_output=True, check=False)  # noqa: S603, S607
        u.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)  # noqa: S603, S607
        return True
    p = plist_path(runtime)
    if not p.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True, check=False)  # noqa: S603, S607
    p.unlink()
    return True


def autostart_status(runtime: str) -> tuple[bool, str]:
    if sys.platform.startswith("linux"):
        u = unit_path(runtime)
        if not u.exists():
            return False, "not installed"
        r = subprocess.run(["systemctl", "--user", "is-active", u.name], capture_output=True, text=True, check=False)  # noqa: S603, S607
        state = r.stdout.strip() or "unknown"
        return state == "active", state
    if sys.platform != "darwin":
        return False, "not supported on this OS"
    p = plist_path(runtime)
    if not p.exists():
        return False, "not installed"
    r = subprocess.run(["launchctl", "list", label(runtime)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return (r.returncode == 0), ("loaded" if r.returncode == 0 else "installed but not loaded")
