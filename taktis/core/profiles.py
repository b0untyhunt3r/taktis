"""Model metadata: context windows, pricing, and auto-refresh from API."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Short-name → API model-id mapping (used for API lookups)
# ---------------------------------------------------------------------------
_SHORT_TO_API: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# ---------------------------------------------------------------------------
# Context window sizes per model short-name (tokens).
# Hardcoded defaults — overwritten at startup by refresh_from_api().
# ---------------------------------------------------------------------------
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 1_000_000,
    "sonnet": 1_000_000,
    "haiku": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000

# Maximum output tokens per model.
MODEL_MAX_OUTPUT: dict[str, int] = {
    "opus": 128_000,
    "sonnet": 64_000,
    "haiku": 64_000,
}

# Approximate $/1M token costs for Claude models.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 0.80, "output": 4.0},
}


def get_context_window(model: str | None) -> int:
    """Return the context window size for a model short-name."""
    return MODEL_CONTEXT_WINDOWS.get(model or "sonnet", DEFAULT_CONTEXT_WINDOW)


def estimate_task_cost(
    model: str,
    estimated_input_tokens: int = 50_000,
    estimated_output_tokens: int = 5_000,
) -> float:
    """Estimate cost in USD for a task given a model and token counts."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["sonnet"])
    return (
        estimated_input_tokens * pricing["input"]
        + estimated_output_tokens * pricing["output"]
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Auto-refresh from Anthropic API
# ---------------------------------------------------------------------------

async def refresh_from_api() -> bool:
    """Fetch model metadata from ``GET /v1/models`` and update globals.

    Requires ``ANTHROPIC_API_KEY`` in the environment.  Returns True if
    at least one model was updated, False otherwise (missing key, network
    error, unexpected response — all logged and silently ignored).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set — using hardcoded model profiles")
        return False

    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed — cannot refresh model profiles from API")
        return False

    # Build reverse map: api_id → short_name
    api_to_short = {v: k for k, v in _SHORT_TO_API.items()}

    updated = 0
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                params={"limit": 100},
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        for model in data.get("data", []):
            model_id = model.get("id", "")
            short = api_to_short.get(model_id)
            if short is None:
                continue

            ctx = model.get("max_input_tokens")
            max_out = model.get("max_tokens")

            if ctx is not None and ctx != MODEL_CONTEXT_WINDOWS.get(short):
                old = MODEL_CONTEXT_WINDOWS.get(short)
                MODEL_CONTEXT_WINDOWS[short] = ctx
                logger.info(
                    "Model %s (%s): context window %s → %s",
                    short, model_id, f"{old:,}" if old else "unset", f"{ctx:,}",
                )
                updated += 1
            elif ctx is not None:
                logger.debug("Model %s (%s): context window unchanged at %s", short, model_id, f"{ctx:,}")

            if max_out is not None:
                MODEL_MAX_OUTPUT[short] = max_out

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Anthropic API returned %s fetching models — using hardcoded profiles",
            exc.response.status_code,
        )
        return False
    except Exception as exc:
        logger.warning(
            "Failed to refresh model profiles from API: %s — using hardcoded defaults",
            exc,
        )
        return False

    if updated:
        logger.info("Refreshed %d model profile(s) from Anthropic API", updated)
    else:
        logger.debug("All model profiles match API — no changes needed")
    return updated > 0
