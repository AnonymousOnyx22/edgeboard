"""Odds math: American odds -> implied probability, vig removal, edges.

All functions are pure and unit-tested in tests/test_edge.py.
"""
from __future__ import annotations

import math


def american_to_prob(odds: float | int) -> float:
    """American odds -> implied probability (0..1). +120 -> 0.4545, -110 -> 0.5238."""
    o = float(odds)
    if o == 0:
        raise ValueError("American odds cannot be 0")
    if o > 0:
        return 100.0 / (o + 100.0)
    return -o / (-o + 100.0)


def decimal_from_american(odds: float | int) -> float:
    """American odds -> decimal odds. -110 -> 1.9090, +150 -> 2.50."""
    o = float(odds)
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / -o


def american_from_prob(p: float) -> float:
    """Probability (0..1) -> American odds (rounded to nearest common unit)."""
    p = max(1e-6, min(1 - 1e-6, p))
    if p >= 0.5:
        return -round((100.0 * p) / (1 - p))
    return round((100.0 * (1 - p)) / p)


def remove_vig(prob_away: float, prob_home: float) -> tuple[float, float]:
    """Remove the bookmaker margin from two mutually exclusive outcomes.
    -110/-110 -> (0.5, 0.5). Raises if total implied <= 1 is not true.""" 
    total = prob_away + prob_home
    if total <= 1.0:
        raise ValueError(f"implied probabilities sum to {total:.4f} <= 1 (no vig to remove)")
    return prob_away / total, prob_home / total


def edge(model_prob: float, vigfree_implied: float) -> float:
    """Positive = the model sees more value than the market. In probability points."""
    return model_prob - vigfree_implied


def cover_probability(model_margin_mean: float, model_margin_sd: float, line: float) -> float:
    """P(betted team covers a line). Line is signed for the betted team
    (favorite = negative, e.g. -6.5). Cover if margin + line > 0."""
    if model_margin_sd <= 0:
        return 1.0 if model_margin_mean + line > 0 else 0.0
    z = (0.0 - (model_margin_mean + line)) / model_margin_sd  # Φ((target-mu)/sd)
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def over_probability(model_total_mean: float, model_total_sd: float, line: float) -> float:
    """P(over a total line), totals normally distributed around model mean."""
    if model_total_sd <= 0:
        return 1.0 if model_total_mean > line else 0.0
    z = (line - model_total_mean) / model_total_sd
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def payout_multiplier(odds: float) -> float:
    """Profit per 1 unit staked (vig excluded, so -110 -> 0.9090, +120 -> 1.2)."""
    o = float(odds)
    return o / 100.0 if o > 0 else -100.0 / o