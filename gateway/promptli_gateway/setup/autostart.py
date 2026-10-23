"""Run the gateway at login (macOS launchd first; Linux systemd --user is next per the plan)."""

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


def install_autostart(runtime: str, program: list[str], *, env: dict[str, str] | None = None) -> str:
    """Write and load a LaunchAgent that keeps `program` running. Returns the plist path."""
    if sys.platform != "darwin":
        raise RuntimeError("Autostart currently supports macOS (launchd); Linux/systemd is coming next.")
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
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})},
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
    p = plist_path(runtime)
    if not p.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True, check=False)  # noqa: S603, S607
    p.unlink()
    return True


def autostart_status(runtime: str) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "not supported on this OS yet"
    p = plist_path(runtime)
    if not p.exists():
        return False, "not installed"
    r = subprocess.run(["launchctl", "list", label(runtime)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return (r.returncode == 0), ("loaded" if r.returncode == 0 else "installed but not loaded")
