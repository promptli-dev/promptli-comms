from __future__ import annotations

from promptli_gateway.setup.autostart import label, systemd_unit, unit_path


def test_systemd_unit_quotes_and_restarts(monkeypatch: object) -> None:
    unit = systemd_unit(
        "claude-code",
        ["/opt/venv/bin/python", "-m", "promptli_claude", "run"],
        {"PATH": "/usr/bin", "X": "a b"},
    )
    assert "ExecStart=/opt/venv/bin/python -m promptli_claude run" in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert 'Environment="X=a b"' in unit and "Environment=PATH=/usr/bin" in unit
    assert label("claude-code") == "ai.promptli.claude-code"


def test_unit_path_honours_xdg(monkeypatch: object, tmp_path: object) -> None:
    import os

    os.environ["XDG_CONFIG_HOME"] = str(tmp_path)
    try:
        assert str(unit_path("claude-code")).endswith("systemd/user/ai.promptli.claude-code.service")
        assert str(unit_path("claude-code")).startswith(str(tmp_path))
    finally:
        del os.environ["XDG_CONFIG_HOME"]
