from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import math

from .config import Settings


@dataclass(frozen=True)
class BudgetReservation:
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _positive_finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise RuntimeError(f"{name} must be a finite number greater than 0")
    return number


def validate_ai_budget(settings: Settings) -> None:
    """Fail closed whenever an external AI provider is enabled."""
    if settings.ai_provider == "none":
        return
    if not settings.ai_budget_enabled:
        raise RuntimeError("AI budget guard must be enabled before an AI provider can start")

    monthly_budget = _positive_finite(settings.ai_monthly_budget_usd, "ai_monthly_budget_usd")
    monthly_stop = _positive_finite(settings.ai_monthly_stop_usd, "ai_monthly_stop_usd")
    _positive_finite(settings.ai_input_cost_per_million_usd, "ai_input_cost_per_million_usd")
    _positive_finite(settings.ai_output_cost_per_million_usd, "ai_output_cost_per_million_usd")

    if monthly_stop >= monthly_budget:
        raise RuntimeError("ai_monthly_stop_usd must be lower than ai_monthly_budget_usd")


def conservative_input_tokens(prompt: str, protocol_overhead_tokens: int = 512) -> int:
    """Return a deliberately high pre-call input-token reservation.

    One token per UTF-8 byte is already conservative for normal model tokenizers.
    A fixed protocol allowance additionally covers role/message framing and other
    provider-side request overhead that is not present in the visible prompt.
    """
    return max(1, len(prompt.encode("utf-8"))) + max(0, int(protocol_overhead_tokens))


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million_usd: float,
    output_cost_per_million_usd: float,
) -> float:
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    amount = (
        Decimal(input_tokens) * Decimal(str(input_cost_per_million_usd))
        + Decimal(output_tokens) * Decimal(str(output_cost_per_million_usd))
    ) / Decimal(1_000_000)
    return float(amount.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING))


def reservation_for_prompt(
    prompt: str,
    max_output_tokens: int,
    settings: Settings,
) -> BudgetReservation:
    input_tokens = conservative_input_tokens(prompt)
    output_tokens = max(0, int(max_output_tokens))
    return BudgetReservation(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_cost_usd(
            input_tokens,
            output_tokens,
            settings.ai_input_cost_per_million_usd,
            settings.ai_output_cost_per_million_usd,
        ),
    )


def month_bounds_utc(now_ts: float | None = None) -> tuple[float, float, str]:
    if now_ts is None:
        now = datetime.now(tz=timezone.utc)
    else:
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return start.timestamp(), end.timestamp(), start.strftime("%Y-%m")
