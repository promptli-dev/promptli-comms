from __future__ import annotations

from typing import Any

import httpx
import respx
from typer.testing import CliRunner

from promptli import AsyncPromptli
from promptli.cli.main import app, mmss

BASE = "https://api.test.invalid"
KEY = "psk_live_abcdefghijkl_" + "a" * 43


def usage_body() -> dict[str, Any]:
    tier = {
        "ratio": 1,
        "price_cents_per_minute": 5,
        "calls": 0,
        "seconds": 0,
        "billed_seconds": 0,
        "pool_seconds": 0,
    }
    return {
        "period": {"key": "2026-10", "start": "2026-10-01T00:00:00.000Z", "end": "2026-11-01T00:00:00.000Z"},
        "plan": "developer",
        "env": "live",
        "metered": True,
        "tiers": [
            {
                **tier,
                "tier": "standard",
                "label": "Standard",
                "calls": 2,
                "seconds": 61,
                "billed_seconds": 66,
                "pool_seconds": 66,
            },
            {
                **tier,
                "tier": "hosted",
                "label": "Hosted",
                "ratio": 2,
                "price_cents_per_minute": 10,
                "calls": 1,
                "seconds": 125,
                "billed_seconds": 126,
                "pool_seconds": 252,
            },
            {**tier, "tier": "premium", "label": "Premium", "ratio": 5, "price_cents_per_minute": 25},
        ],
        "pool": {"included_seconds": 18000, "used_seconds": 318, "remaining_seconds": 17682},
        "overage": {"allowed": True, "pool_seconds": 0, "amount_cents": 0},
    }


def test_mmss() -> None:
    assert [mmss(0), mmss(66), mmss(252), mmss(3725)] == ["0:00", "1:06", "4:12", "62:05"]


@respx.mock
async def test_usage_passes_period_and_parses() -> None:
    route = respx.get(f"{BASE}/v1/usage").mock(return_value=httpx.Response(200, json=usage_body()))
    async with AsyncPromptli(KEY, base_url=BASE) as p:
        u = await p.usage("2026-10")
    assert route.calls[0].request.url.params["period"] == "2026-10"
    assert u.tiers[1].pool_seconds == 252 and u.pool.remaining_seconds == 17682


@respx.mock
def test_cli_usage_prints_minutes(monkeypatch: Any) -> None:
    respx.get(f"{BASE}/v1/usage").mock(return_value=httpx.Response(200, json=usage_body()))
    monkeypatch.setenv("PROMPTLI_API_KEY", KEY)
    monkeypatch.setenv("PROMPTLI_BASE_URL", BASE)
    out = CliRunner().invoke(app, ["usage"], env={"COLUMNS": "200"}).output
    assert "Hosted" in out and "2:06" in out and "4:12" in out  # billed vs counts-as
    assert "5:18 of 300:00 used" in out and "294:42 left" in out


@respx.mock
def test_cli_call_show_prints_billed_segments(monkeypatch: Any) -> None:
    call = {
        "id": "call_1",
        "identity": {"id": "idn_1", "handle": "glow"},
        "direction": "inbound",
        "from": "+15550109999",
        "to": "+15550120001",
        "status": "completed",
        "mode": "agent",
        "outcome": "answered_by_agent",
        "hangup_reason": None,
        "ended_by": "caller",
        "answered_by": None,
        "contact": None,
        "reason": None,
        "recording": None,
        "tier": "hosted",
        "billing": {
            "seconds": 185,
            "billed_seconds": 186,
            "pool_seconds": 312,
            "metered": True,
            "segments": [
                {
                    "tier": "hosted",
                    "engine": "hosted_cascaded",
                    "started_at": "2026-10-24T00:00:00Z",
                    "ended_at": "2026-10-24T00:02:05Z",
                    "seconds": 125,
                    "billed_seconds": 126,
                    "pool_seconds": 252,
                },
                {
                    "tier": "standard",
                    "engine": "mode_c",
                    "started_at": "2026-10-24T00:02:05Z",
                    "ended_at": "2026-10-24T00:03:05Z",
                    "seconds": 60,
                    "billed_seconds": 60,
                    "pool_seconds": 60,
                },
            ],
        },
        "started_at": "2026-10-24T00:00:00Z",
        "answered_at": "2026-10-24T00:00:00Z",
        "ended_at": "2026-10-24T00:03:05Z",
        "duration_sec": 185,
        "env": "live",
    }
    respx.get(f"{BASE}/v1/calls/call_1").mock(return_value=httpx.Response(200, json=call))
    monkeypatch.setenv("PROMPTLI_API_KEY", KEY)
    monkeypatch.setenv("PROMPTLI_BASE_URL", BASE)
    out = CliRunner().invoke(app, ["call", "show", "call_1"], env={"COLUMNS": "200"}).output
    assert "Hosted · talk 2:05 · billed 2:06 (counts as 4:12)" in out
    assert "Standard · talk 1:00 · billed 1:00" in out and "counts as 1:00" not in out
