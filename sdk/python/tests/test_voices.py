"""Voices for both engines, demo clips, and choosing the assistant engine from the SDK and CLI."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from typer.testing import CliRunner

from promptli import AsyncPromptli
from promptli.cli import main as cli

BASE = "https://api.test.invalid"
KEY = "psk_live_abcdefghijkl_" + "a" * 43

VOICES = [
    {
        "id": "marin", "name": "Marin", "description": "Warm", "language": None, "gender": "feminine",
        "provider": "openai", "engines": ["realtime", "realtime_mini"], "preview_url": "/v1/voices/openai/marin/preview",
    },
    {
        "id": "c1", "name": "Priya", "description": "Indian English", "language": "en", "gender": "feminine",
        "provider": "cartesia", "engines": ["cascaded", "agent"], "preview_url": "/v1/voices/cartesia/c1/preview",
    },
]  # fmt: skip

SETTINGS: dict[str, Any] = {
    "incoming_action": "hosted", "forward_to": None, "agent_unreachable": "voicemail", "tts_provider": "cartesia",
    "voice_id": "c1", "language": "en", "speed": None, "hosted_engine": None, "realtime_voice": "marin",
    "recording": False, "fillers": True, "greeting": None,
}  # fmt: skip


@respx.mock
async def test_sdk_lists_voices_by_provider_and_fetches_a_clip() -> None:
    listing = respx.get(f"{BASE}/v1/voices").mock(return_value=httpx.Response(200, json={"data": VOICES[:1]}))
    respx.get(f"{BASE}/v1/voices/openai/marin/preview").mock(
        return_value=httpx.Response(
            200, json={"url": "https://files.test/m.wav", "content_type": "audio/wav"}
        )
    )
    async with AsyncPromptli(KEY, base_url=BASE) as p:
        voices = await p.calls.voices(provider="openai")
        clip = await p.calls.voice_preview("openai", "marin")
    assert listing.calls.last.request.url.params["provider"] == "openai"
    assert voices[0].engines == ["realtime", "realtime_mini"] and voices[0].language is None
    assert clip.url == "https://files.test/m.wav"


@respx.mock
def test_cli_sets_the_engine_and_realtime_voice(monkeypatch: Any) -> None:
    monkeypatch.setenv("PROMPTLI_API_KEY", KEY)
    monkeypatch.setenv("PROMPTLI_BASE_URL", BASE)
    respx.get(f"{BASE}/v1/identities/glow/voice").mock(return_value=httpx.Response(200, json=SETTINGS))
    put = respx.put(f"{BASE}/v1/identities/glow/voice").mock(
        return_value=httpx.Response(
            200, json={**SETTINGS, "hosted_engine": "realtime_mini", "realtime_voice": "cedar"}
        )
    )
    out = CliRunner().invoke(
        cli.app, ["call", "voice", "-i", "glow", "--engine", "realtime-mini", "--realtime-voice", "cedar"]
    )
    assert out.exit_code == 0, out.output
    body = json.loads(put.calls.last.request.content)
    assert (
        body["hosted_engine"] == "realtime_mini"
        and body["realtime_voice"] == "cedar"
        and body["voice_id"] == "c1"
    )


@respx.mock
def test_cli_language_picks_a_cartesia_voice_and_plays_a_clip(monkeypatch: Any) -> None:
    monkeypatch.setenv("PROMPTLI_API_KEY", KEY)
    monkeypatch.setenv("PROMPTLI_BASE_URL", BASE)
    listing = respx.get(f"{BASE}/v1/voices").mock(return_value=httpx.Response(200, json={"data": VOICES}))
    respx.get(f"{BASE}/v1/voices/cartesia/c1/preview").mock(
        return_value=httpx.Response(
            200, json={"url": "data:audio/wav;base64,UklGRg==", "content_type": "audio/wav"}
        )
    )
    played: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_play", lambda url, ct: played.append((url, ct)))
    out = CliRunner().invoke(cli.app, ["call", "voices", "--play", "c1"])
    assert out.exit_code == 0, out.output
    assert played == [("data:audio/wav;base64,UklGRg==", "audio/wav")]
    # --language picks a matching Cartesia voice (OpenAI voices speak any language, so they don't count).
    respx.get(f"{BASE}/v1/identities/glow/voice").mock(return_value=httpx.Response(200, json=SETTINGS))
    respx.put(f"{BASE}/v1/identities/glow/voice").mock(return_value=httpx.Response(200, json=SETTINGS))
    CliRunner().invoke(cli.app, ["call", "voice", "-i", "glow", "--language", "hi"])
    assert listing.calls.last.request.url.params["provider"] == "cartesia"
