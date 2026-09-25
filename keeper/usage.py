"""Measured usage and cost of each analysis.

Every model call records what it actually consumed (tokens, cache, web searches and fetches,
wall time), priced from the table below. List prices in USD; override or extend with
KEEPER_PRICING='{"model-id": [input_per_mtok, output_per_mtok], ...}'.
"""

import json
import os
from dataclasses import asdict, dataclass

# USD per million tokens (input, output). Anthropic list prices.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE_MULT = 1.25   # 5-minute cache writes
CACHE_READ_MULT = 0.10
BATCH_MULT = 0.50         # Message Batches: half price on tokens
WEB_SEARCH_USD = 0.01     # $10 per 1,000 searches; web fetch has no per-call fee


def _prices() -> dict[str, tuple[float, float]]:
    prices = dict(PRICES)
    try:
        prices.update({k: tuple(v) for k, v in json.loads(os.environ.get("KEEPER_PRICING") or "{}").items()})
    except (ValueError, TypeError):
        pass
    return prices


def price_for(model: str) -> tuple[float, float] | None:
    prices = _prices()
    if model in prices:
        return prices[model]
    # tolerate dated/suffixed ids, e.g. "claude-opus-5-20260101" or "claude-opus-5[1m]"
    for known in sorted(prices, key=len, reverse=True):
        if model.startswith(known):
            return prices[known]
    return None


@dataclass
class Run:
    """One analyzer invocation (one model, one mode). Several can belong to one screenshot."""
    analyzer: str                 # claude | local | ocr
    model: str = ""
    mode: str = "realtime"        # realtime | batch | local
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    requests: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    ok: bool = True

    def add_claude_usage(self, usage, model: str | None = None) -> None:
        """Accumulate an Anthropic `usage` object (one API response)."""
        if model:
            self.model = model
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_searches += getattr(server, "web_search_requests", 0) or 0
            self.web_fetches += getattr(server, "web_fetch_requests", 0) or 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost_usd"] = round(d["cost_usd"], 6)
        return d


def claude_cost(run: Run) -> float:
    prices = price_for(run.model)
    if prices is None:
        return 0.0
    inp, out = (p / 1_000_000 for p in prices)
    mult = BATCH_MULT if run.mode == "batch" else 1.0
    tokens = (
        run.input_tokens * inp
        + run.cache_write_tokens * inp * CACHE_WRITE_MULT
        + run.cache_read_tokens * inp * CACHE_READ_MULT
        + run.output_tokens * out
    )
    return tokens * mult + run.web_searches * WEB_SEARCH_USD


def local_cost(duration_ms: int, cost_per_hour: float) -> float:
    """Electricity/amortization estimate for on-prem inference (KEEPER_LOCAL_COST_PER_HOUR)."""
    return duration_ms / 3_600_000 * cost_per_hour
