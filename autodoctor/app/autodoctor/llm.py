from __future__ import annotations

import json
from typing import Any

import aiohttp

from .config import Settings
from .models import Analysis

_SYSTEM = """You are AutoDoctor, a conservative Home Assistant reliability engineer.
Diagnose the supplied incident using only the evidence provided. Prefer the smallest safe change.
Never invent entity IDs, services, files, integrations, or state. Never request or expose secrets.
Treat changes to presence, sleep, climate, power shutdown, security, locks, alarms, databases,
credentials, networking, deletion, and broad automation behavior as medium/high risk.
A low-risk repair must be narrow, reversible, evidence-backed, and behavior-preserving.
If evidence is insufficient, say so and choose observe or propose_fix rather than guessing.
Return JSON only with keys: summary, root_cause, confidence, risk, action, affected_files, checks,
proposed_changes. confidence is 0..1. risk is low|medium|high. action is ignore|observe|propose_fix.
proposed_changes is a list of objects describing changes; do not claim a change has been applied."""


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _analysis(data: dict[str, Any]) -> Analysis:
    confidence = float(data.get("confidence", 0))
    confidence = max(0.0, min(1.0, confidence))
    risk = str(data.get("risk", "high")).lower()
    if risk not in {"low", "medium", "high"}:
        risk = "high"
    action = str(data.get("action", "observe")).lower()
    if action not in {"ignore", "observe", "propose_fix"}:
        action = "observe"
    return Analysis(
        summary=str(data.get("summary", "No summary returned")),
        root_cause=str(data.get("root_cause", "Unknown")),
        confidence=confidence,
        risk=risk,
        action=action,
        affected_files=[str(x) for x in data.get("affected_files", [])][:20],
        checks=[str(x) for x in data.get("checks", [])][:30],
        proposed_changes=[x for x in data.get("proposed_changes", []) if isinstance(x, dict)][:20],
        raw=data,
    )


class BaseProvider:
    async def analyze(self, prompt: str) -> Analysis | None:
        return None

    async def close(self) -> None:
        return None


class NoProvider(BaseProvider):
    pass


class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str, model: str, effort: str) -> None:
        if not model.strip():
            raise RuntimeError("ai_model must be set explicitly when ai_provider=openai")
        self.model = model.strip()
        self.effort = effort
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    async def analyze(self, prompt: str) -> Analysis:
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": _SYSTEM,
            "input": prompt,
            "max_output_tokens": 4000,
            "reasoning": {"effort": self.effort},
        }
        async with self.session.post("https://api.openai.com/v1/responses", json=body, timeout=120) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"OpenAI HTTP {response.status}: {raw[:500]}")
            data = json.loads(raw)
        text = data.get("output_text")
        if not text:
            parts: list[str] = []
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"} and content.get("text"):
                        parts.append(content["text"])
            text = "\n".join(parts)
        return _analysis(_parse_json(text or "{}"))

    async def close(self) -> None:
        await self.session.close()


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str, model: str, effort: str) -> None:
        if not model.strip():
            raise RuntimeError("ai_model must be set explicitly when ai_provider=anthropic")
        self.model = model.strip()
        self.effort = effort
        self.session = aiohttp.ClientSession(
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )

    async def analyze(self, prompt: str) -> Analysis:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4000,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with self.session.post("https://api.anthropic.com/v1/messages", json=body, timeout=120) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Anthropic HTTP {response.status}: {raw[:500]}")
            data = json.loads(raw)
        if data.get("stop_reason") == "refusal":
            raise RuntimeError("Anthropic declined the analysis request")
        text = "\n".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        return _analysis(_parse_json(text or "{}"))

    async def close(self) -> None:
        await self.session.close()


def build_provider(settings: Settings) -> BaseProvider:
    if settings.ai_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("ai_provider=openai but openai_api_key is empty")
        return OpenAIProvider(settings.openai_api_key, settings.ai_model, settings.ai_effort)
    if settings.ai_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ai_provider=anthropic but anthropic_api_key is empty")
        return AnthropicProvider(settings.anthropic_api_key, settings.ai_model, settings.ai_effort)
    return NoProvider()
