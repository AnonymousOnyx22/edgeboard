"""Episode generator: approved picks -> slides + caption + ZIP.
Orchestrates: captions + card_renderer + buffer_pack + DB updates.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import jinja2
import yaml

from ..db import database as db
from . import captions, card_renderer, episode_manager as em, buffer_pack

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT
_BRAND_FILE = yaml.safe_load((PROJECT_ROOT / "config" / "brand.yaml").read_text(encoding="utf-8"))
BRAND = _BRAND_FILE["brand"]
DISCLAIMER = _BRAND_FILE.get("disclaimer", "19+ | Please gamble responsibly")
CTA = _BRAND_FILE.get("cta", {})
HOOKS = captions.load_hooks()


def generate_episode(ep_type: str, ep_date: str | None = None,
                     picks_ids: list[int] | None = None,
                     sport: str = "nfl", hook: str | None = None,
                     note: str = "",
                     db_path=db.DEFAULT_DB) -> dict:
    """Main entrypoint:
    1. create_or_regenerate draft
    2. build caption + card contexts from picks
    3. render slides + caption
    4. pack output folder + ZIP
    5. update episode status -> 'generated'
    Returns the updated Episode as a dict.
    """
    global _BRAND_FILE, BRAND, DISCLAIMER, CTA, HOOKS
    _BRAND_FILE = yaml.safe_load(
        (PROJECT_ROOT / "config" / "brand.yaml").read_text(encoding="utf-8")
    )
    BRAND = _BRAND_FILE["brand"]
    DISCLAIMER = _BRAND_FILE.get("disclaimer", "19+ | Please gamble responsibly")
    CTA = _BRAND_FILE.get("cta", {})
    HOOKS = captions.load_hooks()

    ep_date = ep_date or dt.date.today().isoformat()
    ep = em.create_or_regenerate(ep_type, ep_date, sport=sport, db_path=db_path)
    em.link_picks(ep.id, picks_ids or [], db_path)

    picks = (_load_picks(picks_ids, db_path) if picks_ids is not None
             else _picks_for_date(ep_date, sport, db_path))
    hook_label = hook or captions.pick_hook(ep_type, sport, db_path) or ep_type
    ep.hook_key = hook_label
    ctx = captions.analysis_ctx(picks)
    ctx["date"] = ep_date
    ctx["sport_label"] = sport.upper()
    ctx["sport"] = sport
    ctx["slate_label"] = _slate_label(picks)
    ctx["ep_number"] = ep.number
    ctx["note"] = note or ""
    record = captions.record_ctx(db_path, sport)
    record["season"] = dt.date.today().year
    record["sport_label"] = sport.upper()
    record["sport_record"] = f"{record['wins']}-{record['losses']}" if (record["wins"] or record["losses"]) else "0-0"
    record["sport_hit"] = record["hit_rate"]
    ctx["record"] = record

    hook_rendered = _render_hook(hook_label, ctx)
    slides, caption = _slide_build(ep_type, ep, picks, ctx, record, hook_rendered)
    captions.validate_caption(caption, _BRAND_FILE)

    alt_text = captions.alt_text(ep_type, ep.number, ctx)
    hashtag_txt = captions.hashtags(sport, _BRAND_FILE)
    output_dir = ROOT / "output" / f"{ep_date}_{ep_type}_{ep.number:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)

    pack = buffer_pack.BufferPack(BRAND)
    pack.render(slides, output_dir)
    if ep_type in ("picks", "upset_alert", "biggest_edge") and slides:
        hook_ctx = dict(slides[0][1])
        hook_ctx["hook"] = hook_rendered
        try:
            pack.story(hook_ctx, output_dir)
        except Exception:
            pass  # story is nice-to-have
        try:
            pack.reel(slides, output_dir)
        except Exception:
            pass
    pack.write_text(output_dir, caption, hashtag_txt, alt_text)

    meta = {"episode_id": ep.id, "number": ep.number, "type": ep.ep_type,
            "date": ep_date, "slate": ep.slate, "status": "generated",
            "linked_pick_ids": picks_ids or ep.linked_pick_ids,
            "slides": sorted(p.name for p in output_dir.glob("*.png")),
            "reel": sorted(p.name for p in (output_dir / "reel").glob("*.png")) if (output_dir / "reel").exists() else [],
            "caption.txt": caption, "hashtags.txt": hashtag_txt, "alt_text.txt": alt_text,
            "output_dir": str(output_dir)}
    pack.write_episode_json(output_dir, meta)
    _update_episode_record(ep, caption, hashtag_txt, alt_text, output_dir, db_path)
    em.set_status(ep.id, "generated", db_path)
    for pick_id in picks_ids or []:
        db.execute(db_path, """UPDATE picks SET status='content_generated'
                   WHERE id=? AND status IN ('approved','edited')""", (pick_id,))
    try:
        zip_path = pack.zip(output_dir)
    except Exception:
        zip_path = None
    return {"id": ep.id, "number": ep.number, "type": ep_type,
            "date": ep_date, "output_dir": str(output_dir),
            "zip": str(zip_path) if zip_path else None, "n_slides": len(slides)}


def save_caption(episode_id: int, text: str, db_path=db.DEFAULT_DB) -> None:
    """Validate and persist an edited caption, including generated pack files."""
    brand_file = yaml.safe_load(
        (PROJECT_ROOT / "config" / "brand.yaml").read_text(encoding="utf-8")
    )
    captions.validate_caption(text, brand_file)
    ep = em.get_episode(episode_id, db_path)
    if not ep:
        raise ValueError(f"no episode {episode_id}")
    db.execute(db_path, "UPDATE episodes SET caption_text=? WHERE id=?",
               (text, episode_id))
    if not ep.output_dir:
        return
    output_dir = Path(ep.output_dir)
    if not output_dir.is_dir():
        return
    (output_dir / "caption.txt").write_text(text, encoding="utf-8")
    meta_path = output_dir / "episode.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["caption.txt"] = text
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    buffer_pack.BufferPack(brand_file["brand"]).zip(output_dir)


def _picks_for_date(date: str, sport: str, db_path) -> list[dict]:
    return [dict(r) for r in db.query(db_path, """
        SELECT p.*, l.team, l.market, l.line, l.odds_american AS odds, l.game_id,
               g.home_team, g.away_team, ht.abbrev AS home, at.abbrev AS away
        FROM picks p JOIN pick_legs l ON l.pick_id=p.id
        JOIN games g ON g.id=l.game_id
        JOIN teams ht ON ht.id=g.home_team
        JOIN teams at ON at.id=g.away_team
        WHERE p.status IN ('approved','edited')
          AND COALESCE(g.game_date, date(g.start_utc))=date(?)
          AND p.sport_id=(SELECT id FROM sports WHERE code=?)
        ORDER BY p.confidence_final DESC, l.ord""", (date, sport))]


def _load_picks(pick_ids: list[int], db_path) -> list[dict]:
    out = []
    for pid in pick_ids:
        row = db.query(db_path, """SELECT p.*, r.result, r.units_won
                        FROM picks p LEFT JOIN results r ON r.pick_id=p.id
                        WHERE p.id=?""", (pid,))
        if not row:
            continue
        leg = db.query(db_path, "SELECT * FROM pick_legs WHERE pick_id=? ORDER BY ord LIMIT 1", (pid,))
        # Keep the pick's id/status when columns overlap with its leg.
        pick = {**(dict(leg[0]) if leg else {}), **dict(row[0])}
        if pick.get("odds") is None and pick.get("odds_american") is not None:
            pick["odds"] = pick["odds_american"]
        if pick.get("matchup") is None and pick.get("game") is None:
            g = db.query(db_path, """SELECT g.id, ht.name||' @ '||at.name AS matchup,
                                    g.slate FROM games g
                                    JOIN teams ht ON ht.id=g.home_team
                                    JOIN teams at ON at.id=g.away_team
                                    WHERE g.id=?""", (pick.get("game_id"),))
            if g:
                pick["matchup"] = g[0]["matchup"]
                pick["slate"] = pick.get("slate") or g[0]["slate"]
        out.append(pick)
    return out


def _slate_label(picks: list[dict]) -> str:
    slates = {p.get("slate") for p in picks if p.get("slate")}
    if len(slates) == 1:
        return next(iter(slates)).replace("_", " ").title()
    return ""


def _slide_build(ep_type: str, ep: em.Episode, picks: list[dict],
                 ctx: dict, record: dict, hook: str) -> tuple[list[dict], str]:
    """Return (slides list for card_renderer, caption text)."""
    slug = str(ep.number).zfill(3)
    ep_ref = {"number": ep.number, "date": ctx.get("date", "")}
    base = {"ep": ep_ref, "brand": BRAND, "disclaimer": DISCLAIMER,
            "sport": ctx.get("sport", ""), "sport_label": ctx.get("sport_label", ""),
            "slate_label": ctx.get("slate_label", ""), "date": ctx.get("date", "")}
    match ep_type:
        case "picks":
            slides = _picks_slides(picks, ctx, record, hook, slug)
        case "recap":
            slides = _recap_slides(picks, ctx, record, slug)
        case "weekly":
            slides = [("weekly", {**base, **ctx, **record, "note": ctx.get("note", "")})]
        case "nfl_cheatsheet":
            slides = [("cheatsheet", {**base, **ctx})]
        case "upset_alert":
            slides = _upset_slide(picks, ctx, base)
        case "early_lines":
            slides = [("early_lines", {**base, **ctx})]
        case "biggest_edge":
            slides = _biggest_edge_slide(picks, ctx, base)
        case "no_plays":
            slides = [("no_plays", {**base, "note": ctx.get("note", "")})]
        case _:
            slides = [("special", {**base, "emoji": "⭐", "hook": hook,
                                   "subtitle": ctx.get("date", "")})]
    # render caption
    tpl_name = _caption_tpl_name(ep_type)
    try:
        caption = captions.render_caption(tpl_name, {"ep": ep, "ctx": ctx, "record": record,
                                                     "brand": BRAND, "disclaimer": DISCLAIMER})
    except jinja2.TemplateNotFound:
        caption = f"Episode {ep.number}  {ep_type}\n\n{DISCLAIMER}"
    return slides, caption


def _picks_slides(picks, ctx, record, hook, slug):
    slides = [("hook", {
        "hook": hook.replace("{day}", ctx.get("slate_label", "")),
        "sport_label": ctx.get("sport_label", ""),
        "sport": ctx.get("sport_label", "").lower(),
        "ep": {"number": ctx.get("ep_number", ""), "date": ctx.get("date", "")},
        "brand": BRAND, "disclaimer": DISCLAIMER,
        "combined_odds": ctx.get("combined_odds", ""),
        "top_conf": ctx.get("top_conf", ""),
    })]
    n = len(picks)
    for i, p in enumerate(picks, 1):
        slides.append(("leg", {
            "leg_num": i, "total_legs": n,
            "matchup": p.get("matchup", p.get("game", "")),
            "bet_label": f"{p['team']} {p['market'].upper()}" + (f" {p['line']:+.1f}" if p.get("line") else ""),
            "odds": ctx["plays"][i-1]["odds"] if i <= len(ctx["plays"]) else "",
            "conf_pct": float(p.get("confidence_final") or p.get("confidence_model") or 5) * 10,
            "conf_display": ctx["plays"][i-1]["conf"] if i <= len(ctx["plays"]) else "",
            "support_stat": "", "reason": p.get("reason", ""),
            "slate_label": ctx.get("slate_label", ""),
            "sport": ctx.get("sport_label", "").lower(),
            "ep": {"number": ctx.get("ep_number", ""), "date": ctx.get("date", "")},
            "brand": BRAND, "disclaimer": DISCLAIMER,
        }))
    slides.append(("record", {
        "record": record, "sport_label": ctx.get("sport_label", ""),
        "ep": {"number": ctx.get("ep_number", ""), "date": ctx.get("date", "")},
        "brand": BRAND, "disclaimer": DISCLAIMER,
    }))
    slides.append(("cta", {
        "brand": BRAND, "disclaimer": DISCLAIMER,
        "cta": CTA, "sport": ctx.get("sport_label", "").lower(),
    }))
    return slides


def _recap_slides(picks, ctx, record, slug):
    results = []
    for p in picks:
        r = p.get("result", "W")
        icon = {"W": "✅", "L": "❌", "P": "➖"}.get(r, "❓")
        results.append({"icon": icon, "label": f"{p['team']} {p['market'].upper()}", "result": r})
    missed = [f"{p['team']} {p['market'].upper()}" for p in picks if p.get("result") == "L"]
    return [("recap_leg", {
        "recap_date": ctx.get("date", ""),
        "legs": results, "missed": "Missed: " + ", ".join(missed) if missed else "Clean sweep.",
        "sport_label": ctx.get("sport_label", ""),
        "ep": {"number": ctx.get("ep_number", ""), "date": ctx.get("date", "")},
        "brand": BRAND, "disclaimer": DISCLAIMER,
    })]


def _upset_slide(picks, ctx, base):
    p = picks[0] if picks else {}
    ctx2 = dict(base)
    ctx2.update({
        "hook": ctx.get("reason", "I'm going against the public"),
        "matchup": p.get("matchup", p.get("game", "")),
        "bet_label": f"{p.get('team','')} {p.get('market','').upper()}",
        "odds": ctx["plays"][0]["odds"] if ctx.get("plays") else "",
        "reason": p.get("reason", ""),
    })
    return [("upset", ctx2)]


def _biggest_edge_slide(picks, ctx, base):
    p = picks[0] if picks else {}
    ctx2 = dict(base)
    ctx2.update({
        "hook": ctx.get("reason", "This edge is too big to ignore"),
        "matchup": p.get("matchup", p.get("game", "")),
        "bet_label": f"{p.get('team','')} {p.get('market','').upper()}",
        "odds": ctx["plays"][0]["odds"] if ctx.get("plays") else "",
        "edge_label": f"Confidence: {p.get('confidence_final', p.get('confidence_model', 0))}/10 · Edge: {p.get('edge', 0)*100:.1f}%",
        "reason": p.get("reason", ""),
    })
    return [("biggest_edge", ctx2)]


def _caption_tpl_name(ep_type: str) -> str:
    templates = captions.caption_templates()
    opts = templates.get(ep_type, [])
    if opts:
        import random
        return random.choice(opts)
    return f"{ep_type}_1"


def _render_hook(hook: str, ctx: dict) -> str:
    fmt_args = {
        "day": ctx.get("slate_label", "") or "today",
        "week": _current_nfl_week(),
        "sport": ctx.get("sport_label", "").lower(),
        "fav": next((p.get("team", "") for p in ctx.get("plays", [])), "the favorite"),
    }
    try:
        return hook.format(**fmt_args)
    except (KeyError, IndexError):
        return hook


def _current_nfl_week() -> int:
    try:
        return int(db.get_config().get("nfl_week", 1))
    except Exception:
        return 1


def _update_episode_record(ep, caption, hashtags, alt_text, output_dir, db_path) -> None:
    db.execute(db_path, """UPDATE episodes SET template_key=?, hook_key=?,
                caption_text=?, hashtags=?, alt_text=?, output_dir=? WHERE id=?""",
               ("", f"{ep.hook_key}|{dt.date.today().isoformat()}", caption,
                hashtags, alt_text, str(output_dir), ep.id))
