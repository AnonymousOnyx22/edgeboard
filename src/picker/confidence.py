"""Confidence scoring: 1-10 from edge, model probability, and data quality.

    Confidence = clamp(round( wE*edge_score + wP*prob_score + wQ*quality_score ), 1, 10)

All functions take the `confidence:` block from config/settings.yaml as their
config dict (plus an optional edge target from the `edge:` block). The
dashboard shows the breakdown privately; only the final score is public.
"""
from __future__ import annotations

from ..db.database import get_config


def conf_cfg() -> dict:
    return get_config()["confidence"]


def edge_target(cfg=None) -> float:
    return float((cfg or get_config())["edge"]["target"])


def edge_score(edge, target: float = 0.03) -> float:
    """Map an edge to 0-10, where ``target`` is the full-scale edge."""
    if target <= 0:
        return 0.0
    return max(0.0, min(10.0, 10.0 * (float(edge) / target)))


def prob_score(model_prob: float) -> float:
    """0-10. 50% (coin flip) -> 0, 75% -> 10."""
    scaled = (float(model_prob) - 0.50) * 40.0
    return max(0.0, min(10.0, scaled))


def quality_score(quality: dict, cfg: dict | None = None) -> float:
    """Start at 10, subtract configurable penalties per flagged data gap."""
    cfg = cfg or conf_cfg()
    penalties = cfg.get("quality_penalties", {})
    score = 10.0
    for flag, penalty in penalties.items():
        if flag != "small_sample_threshold" and quality.get(flag):
            score -= float(penalty)
    threshold = int(penalties.get("small_sample_threshold", 4))
    if quality.get("games_of_history", 99) < threshold:
        score -= float(penalties.get("early_season_small_sample", 1.0))
    return max(0.0, min(10.0, score))


def compute_confidence(edge, model_prob: float, quality: dict | None = None,
                       cfg: dict | None = None, target: float | None = None) -> dict:
    """Return breakdown dict; `score` is the final 1-10 value."""
    cfg = cfg or conf_cfg()
    target = float(target) if target is not None else edge_target()
    weights = cfg["weights"]
    q = quality or {}
    es = edge_score(edge, target)
    ps = prob_score(model_prob)
    qs = quality_score(q, cfg)
    raw = weights["edge"] * es + weights["probability"] * ps + weights["quality"] * qs
    final = max(1, min(10, round(raw)))
    return {
        "score": final,
        "raw": round(raw, 4),
        "edge_score": round(es, 3),
        "prob_score": round(ps, 3),
        "quality_score": round(qs, 3),
        "edge": round(float(edge), 4),
        "model_prob": round(float(model_prob), 4),
    }


def units_from_confidence(score: float, cfg: dict | None = None) -> float:
    """1u/2u/3u from configurable tiers {units: max_confidence}, sorted asc."""
    settings = cfg or get_config()
    tiers_raw = settings.setdefault("units", {}).get("tiers") or {"1": 5.5, "2": 7.5, "3": 10}
    tiers = sorted(((int(k), float(v)) for k, v in tiers_raw.items()), key=lambda kv: kv[1])
    for units, max_score in tiers:
        if float(score) <= max_score:
            return float(units)
    return float(tiers[-1][0])


def display(score: float, cfg: dict | None = None) -> str:
    """Public rendering: 8/10 (scale), 80% (percent), or ★★★★☆ (stars)."""
    score = float(score)
    mode = (cfg or get_config())["confidence"].get("display", "scale")
    if mode == "percent":
        return f"{round(score * 10)}%"
    if mode == "stars":
        return "★" * round(score) + "☆" * (10 - round(score))
    return f"{round(score)}/10"
