from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    min_level: str = "ERROR"
    min_occurrences_for_ai: int = 2
    analysis_cooldown_seconds: int = 900
    max_ai_analyses_per_hour: int = 12
    ai_provider: str = "none"
    ai_model: str = ""
    ai_effort: str = "low"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    mcp_enabled: bool = False
    mcp_url: str = ""
    mcp_token: str = ""
    notify_on_new_incident: bool = True
    auto_apply_low_risk: bool = False

    @classmethod
    def load(cls, path: str = "/data/options.json") -> "Settings":
        p = Path(path)
        data = json.loads(p.read_text()) if p.exists() else {}
        known = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})
