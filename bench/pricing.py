"""USD per 1M tokens (input, output) for models the benchmark calls directly.

Claude Code reports its own `total_cost_usd`, so it is not priced here.
Checked 2026-09-25; update when providers change prices.
"""

PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Price by the model name without a provider prefix (`openai/gpt-4o-mini` -> `gpt-4o-mini`)."""
    price = PRICES.get(model.split("/")[-1])
    if price is None:
        return None
    return (tokens_in * price[0] + tokens_out * price[1]) / 1e6
