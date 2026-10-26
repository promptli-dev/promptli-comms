"""Conformance with the server contract synced into spec/ (signing vectors, error catalog, operations)."""

from __future__ import annotations

import json
from pathlib import Path

from promptli import errors as E
from promptli.webhooks import verify_webhook

SPEC = Path(__file__).resolve().parents[2] / "spec"


def test_signing_vectors_verify() -> None:
    data = json.loads((SPEC / "fixtures" / "signing" / "vectors.json").read_text())
    assert data["vectors"], "no vectors synced"
    for v in data["vectors"]:
        headers = {
            "X-Promptli-Id": v["id"],
            "X-Promptli-Timestamp": v["timestamp"],
            "X-Promptli-Signature": v["signature_header"],
        }
        assert verify_webhook(v["body"].encode(), headers, v["secret"], now=int(v["timestamp"]))
        assert not verify_webhook(v["body"].encode() + b" ", headers, v["secret"], now=int(v["timestamp"]))


def test_every_error_code_maps_to_a_typed_exception() -> None:
    catalog = json.loads((SPEC / "errors.json").read_text())["errors"]
    for entry in catalog:
        exc = E.error_from_response(entry["status"], {"error": {"code": entry["code"], "message": "x"}})
        assert exc.code == entry["code"]
        assert isinstance(exc, E.PromptliError)
        if entry["status"] == 401:
            assert isinstance(exc, E.AuthenticationError)
        if entry["status"] == 429:
            assert isinstance(exc, E.RateLimitError)


def test_facade_covers_core_operations() -> None:
    """Every core operation in the spec should be reachable from the SDK facade (allowlist the rest)."""
    spec = json.loads((SPEC / "openapi.json").read_text())
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values() if "operationId" in op}
    src = "".join(
        p.read_text() for p in (Path(__file__).resolve().parents[2] / "sdk/python/promptli").glob("*.py")
    )
    path_fragments = {
        "sendEmail": '/mail/messages"',
        "listEmails": '/mail/messages"',
        "replyEmail": "reply",
        "sendText": '/texts"',
        "provisionPhoneNumber": "/phone-number",
        "listEvents": "/v1/events",
        "getChannelStatus": "/channel-status",
        "createIdentity": '/v1/identities"',
        "createWebhookSubscription": "/v1/webhooks/subscriptions",
        "createApiKey": '/v1/api-keys"',
        "verifySignup": "/v1/signup/verify",
        "listConversations": '/conversations"',
        "sandboxInboundText": "/v1/sandbox/texts/inbound",
        "whoami": "/v1/whoami",
        "getUsage": "/v1/usage",
        "createScheduled": '"/scheduled"',
        "cancelScheduled": "/v1/scheduled/",
        "getCallBrief": "/brief",
        "answerCallQuestion": "/answer",
        "sayOnCall": "/say",
        "takeOverCall": "/take-over",
        "handBackCall": "/hand-back",
        "getHostedSettings": '"/hosted"',
    }
    for op, fragment in path_fragments.items():
        assert op in ops, f"{op} missing from spec"
        assert fragment in src, f"SDK has no call for {op}"
