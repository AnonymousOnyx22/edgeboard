"""Caption + hook engine. Editable without Python (Jinja2 in templates/)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import jinja2
import yaml

from ..db import database as db

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
CAPTION_DIR = TEMPLATES_DIR / "captions"
HOOKS_PATH = TEMPLATES_DIR / "hooks.yaml"


class BannedWordError(ValueError):
    pass


def load_hooks() -> dict:
    with open(HOOKS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def caption_templates() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(CAPTION_DIR.glob("*.jinja2")):
        ep_type, suffix = path.stem.rsplit("_", 1)
        if not suffix.isdigit():
            continue
        out.setdefault(ep_type, []).append(path.stem)
    return out


def pick_hook(ep_type: str, sport: str = "", db_path=db.DEFAULT_DB) -> str | None:
    """Pick a hook that was not used for this episode type in the last 2 days.
    Falls back to the first unused one; if all used recently, take the oldest."""
    hooks = load_hooks().get(ep_type, [])
    if not hooks:
        return None
    recent = {}
    rows = db.query(db_path,
                    "SELECT hook_key FROM episodes WHERE ep_type=? ORDER BY date DESC LIMIT 6",
                    (ep_type,))
    for r in rows:
        if r["hook_key"]:
            try:
                day = dt.date.fromisoformat(r["hook_key"].split("|")[1] if "|" in r["hook_key"] else "2000-01-01")
            except ValueError:
                day = dt.date(2000, 1, 1)
            recent[day] = r["hook_key"].split("|")[0]
    # rank hooks by recency of use
    for hook in hooks:
        used_on = [d for d, h in recent.items() if h == hook]
        if used_on and (dt.date.today() - max(used_on)).days <= 2:
            continue
        return hook
    return hooks[0]


def render_caption(template_name: str, context: dict) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(CAPTION_DIR)),
                             autoescape=False, trim_blocks=True, lstrip_blocks=True)
    tmpl = env.get_template(f"{template_name}.jinja2")
    text = tmpl.render(**context).strip()
    return text


def validate_caption(text: str, brand: dict) -> None:
    banned = [w.lower() for w in brand.get("banned_words", [])]
    lowered = text.lower()
    found = [w for w in banned if w in lowered]
    if found:
        raise BannedWordError(f"banned word(s) in caption: {', '.join(found)}")


def hashtags(sport: str, brand: dict) -> str:
    """First-comment hashtag block for a sport."""
    tags = list(brand.get("hashtags", {}).get("default", []))
    tags += brand.get("hashtags", {}).get(sport, [])
    return " ".join(dict.fromkeys(tags))


def alt_text(ep_type: str, ep_number: int, ctx: dict) -> str:
    """Accessibility alt text for the carousel image."""
    n = ctx.get("n_picks")
    if ep_type == "picks":
        plays = "; ".join(f"{p['team']} {p['market']} @ {p['odds']}"
                          for p in ctx.get("plays", [])[:3])
        return f"Episode {ep_number}: my picks for {ctx.get('slate_label', '')}. {plays}."
    if ep_type == "recap":
        return f"Episode {ep_number}: recap slide showing results of recent picks."
    return f"Episode {ep_number} ({ep_type}) from {ctx.get('date', '')}."


# ----------------------------------------------------------------- context
def analysis_ctx(picks: list[dict]) -> dict:
    """Derived stats from a list of pick dicts (for captions/cards)."""
    confs = [p.get("confidence_final") or p.get("confidence_model") or 0 for p in picks]
    odds_list = [p.get("odds") for p in picks]
    return {
        "n_picks": len(picks),
        "players_display": ", ".join(p["team"] for p in picks),
        "top_conf": f"{max(confs)}/10" if confs else "",
        "avg_conf": f"{round(sum(confs) / len(confs))}/10" if confs else "",
        "combined_odds": _combine_display(odds_list),
        "plays": [
            {
                "team": p["team"], "market": p["market"], "line": p.get("line"),
                "odds": _amer_str(p.get("odds")), "conf": f"{int(p.get('confidence_final') or p.get('confidence_model') or 0)}/10",
                "matchup": p.get("matchup", p.get("game", "")),
                "reason": p.get("reason", ""),
            }
            for p in picks
        ],
    }


def record_ctx(db_path=db.DEFAULT_DB, sport: str | None = None) -> dict:
    """Public record straight from the DB - never editable by hand."""
    sql = """
        SELECT r.result, r.units_won,
               COALESCE(p.units_final, p.units_model, 1.0) AS units_staked
        FROM results r JOIN picks p ON p.id=r.pick_id
        WHERE p.status='graded'
    """
    params: tuple = ()
    if sport:
        sql += " AND p.sport_id=(SELECT id FROM sports WHERE code=?)"
        params = (sport,)
    rows = db.query(db_path, sql, params)
    wins = sum(1 for r in rows if r["result"] == "W")
    losses = sum(1 for r in rows if r["result"] == "L")
    pushes = sum(1 for r in rows if r["result"] == "P")
    plays = wins + losses
    units = sum(r["units_won"] for r in rows)
    stake = sum(float(r["units_staked"]) for r in rows)
    return {
        "wins": wins, "losses": losses, "pushes": pushes,
        "hit_rate": f"{wins/plays:.1%}" if plays else "0.0%",
        "units": f"{units:+.1f}", "roi": f"{units/stake:.1%}" if stake else "0.0%",
    }


def _combine_display(odds_list: list) -> str:
    from ..picker.edge import decimal_from_american
    if not odds_list:
        return ""
    dec = 1.0
    for o in odds_list:
        dec *= decimal_from_american(o)
    if dec >= 2.0:
        return f"+{round((dec - 1) * 100)}"
    return str(round(-100.0 / (dec - 1.0)))


def _amer_str(odds) -> str:
    o = int(odds)
    return f"+{o}" if o > 0 else str(o)
