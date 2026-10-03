"""A fake Anthropic client: records every request, returns canned responses.
No network, no key. Mirrors only what ai_review.py touches."""

from __future__ import annotations

import json
from types import SimpleNamespace


class FakeAnthropic:
    def __init__(
        self, findings=None, *, raw=None, exc=None, stop_reason="end_turn", usage=(1500, 400)
    ):
        self.payload = {"findings": findings or []} if raw is None else raw
        self.exc = exc
        self.stop_reason = stop_reason
        self.usage = usage
        self.calls: list[dict] = []
        self.messages = self  # lets code call client.messages.create(...)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=self.usage[0], output_tokens=self.usage[1]),
        )


def factory_for(fake: FakeAnthropic):
    """An ai_client_factory that records the key it was handed."""
    seen: list[str] = []

    def factory(api_key: str):
        seen.append(api_key)
        return fake

    factory.seen = seen  # type: ignore[attr-defined]
    return factory


def ai_finding(path="k8s/app.yaml", line=4, **over):
    base = {
        "path": path,
        "line": line,
        "category": "reliability",
        "confidence": "medium",
        "title": "Single replica",
        "comment": "One replica means downtime during node drains. Consider at least two.",
    }
    base.update(over)
    return base
