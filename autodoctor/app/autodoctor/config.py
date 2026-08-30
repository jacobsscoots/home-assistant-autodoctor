from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    min_level: str = "ERROR"
    min_occurrences_for_ai: int = 2
    max_incidents_retained: int = 5000
    analysis_cooldown_seconds: int = 900
    max_ai_analyses_per_hour: int = 12
    max_ai_analyses_per_family_per_hour: int = 2
    ai_startup_backlog_grace_seconds: int = 300
    ai_provider: str = "none"
    ai_model: str = ""
    ai_effort: str = "low"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ai_budget_enabled: bool = False
    ai_monthly_budget_usd: float = 0.0
    ai_monthly_stop_usd: float = 0.0
    ai_input_cost_per_million_usd: float = 0.0
    ai_output_cost_per_million_usd: float = 0.0
    memory_enabled: bool = True
    memory_seed_enabled: bool = False
    memory_max_items: int = 5
    memory_max_chars: int = 6000
    memory_ai_hypothesis_expiry_days: int = 30
    memory_quiet_outcome_seconds: int = 86400
    memory_worsened_recurrences: int = 10
    mcp_enabled: bool = False
    mcp_url: str = ""
    mcp_token: str = ""
    notify_on_new_incident: bool = True
    auto_apply_low_risk: bool = False

    @classmethod
    def load(cls, path: str = "/data/options.json") -> "Settings":
        p = Path(path)
        data = json.loads(p.read_text()) if p.exists() else {}
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})
