"""Templates: preview every card + caption with clearly labeled SAMPLE data,
edit brand.yaml and hooks.yaml without touching code."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import streamlit as st
import yaml

from src.app.common import render_sidebar
from src.content import captions, card_renderer
from src.db import database as db

ROOT = Path(__file__).resolve().parents[3]
BRAND_TEXT = (ROOT / "config" / "brand.yaml").read_text(encoding="utf-8")
HOOKS_TEXT = (ROOT / "templates" / "hooks.yaml").read_text(encoding="utf-8")
BRAND_FILE = yaml.safe_load(BRAND_TEXT)
BRAND = BRAND_FILE["brand"]
DISCLAIMER = BRAND_FILE["disclaimer"]
CTA = BRAND_FILE["cta"]
PREVIEWS = ROOT / "data" / "_preview"
PREVIEWS.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Templates", layout="wide")
render_sidebar()

st.subheader("Templates")
st.warning("Everything on this page renders with **clearly labeled SAMPLE data** â€” "
           "never use these previews as real content.")

# ---------------------------------------------------------------- sample ctx
SAMPLE_EP = {"number": 42, "date": "2026-09-20", "week": 3, "slate": "Sunday Early"}
SAMPLE_RECORD = {
    "wins": 12, "losses": 7, "pushes": 1, "hit_rate": "63.2%", "units": "+8.7",
    "roi": "+6.4%", "season": 2026, "sport_label": "NFL",
    "sport_record": "12-7", "sport_hit": "63.2%",
}
BASE = {
    "ep": SAMPLE_EP, "brand": BRAND, "disclaimer": DISCLAIMER,
    "sport": "nfl", "sport_label": "NFL", "sport_emoji": "ðŸˆ",
    "slate_label": "Sunday Early", "date": SAMPLE_EP["date"],
}
HOOKS = captions.load_hooks()


def _ctx(template: str) -> dict:
    c = dict(BASE)
    match template:
        case "hook":
            c.update(hook="SAMPLE â€” My Sunday NFL Parlay ðŸŽ¯", combined_odds="+350",
                     top_conf="8/10")
        case "leg":
            c.update(leg_num=1, total_legs=3, matchup="BUF @ DET", bet_label="DET +3",
                     odds="-110", conf_pct=80, conf_display="8/10",
                     support_stat="SAMPLE â€” total team yards 2nd rank",
                     reason="SAMPLE â€” line looks a point off from my numbers.")
        case "record":
            c["record"] = SAMPLE_RECORD
        case "cta":
            c.update(cta=CTA)
        case "recap_leg":
            c.update(recap_date="2026-09-21", legs=[
                {"icon": "âœ…", "label": "KC -3", "result": "W"},
                {"icon": "âŒ", "label": "BUF ML", "result": "L"},
                {"icon": "âœ…", "label": "Over 45.5", "result": "W"},
            ], missed="Missed: BUF ML. Clean otherwise.")
        case "weekly":
            c["record"] = SAMPLE_RECORD
            c["note"] = "SAMPLE â€” one highlighted stat line."
        case "cheatsheet":
            c.update(plays=[
                {"matchup": "BUF @ DET", "bet": "DET +3", "odds": "-110", "conf": "8/10"},
                {"matchup": "KC @ CIN", "bet": "KC -1.5", "odds": "-115", "conf": "7/10"},
                {"matchup": "SF @ SEA", "bet": "Over 48", "odds": "-110", "conf": "6/10"},
            ])
        case "early_lines":
            c.update(lines=[
                {"matchup": "GB @ MIN", "line_label": "MIN -2.5", "note": "opened -1.5"},
                {"matchup": "PHI @ DAL", "line_label": "PHI +140", "note": "sharp move"},
                {"matchup": "KC @ CIN", "line_label": "O 47.5", "note": "public on the over"},
            ])
        case "upset":
            c.update(hook="SAMPLE â€” Upset Alert ðŸš¨", matchup="BUF @ DET", bet_label="DET ML",
                     odds="+190", conf_display="7/10",
                     reason="SAMPLE â€” market still pricing last season.")
        case "biggest_edge":
            c.update(hook="SAMPLE â€” My Biggest Edge Today ðŸ’Ž", matchup="SF @ SEA",
                     bet_label="SF -1.5", odds="-110", edge_label="SAMPLE â€” CF 9/10 Â· Edge 6.2%",
                     reason="SAMPLE â€” line off a full point from my model.")
        case "special":
            c.update(emoji="â­", hook="SAMPLE â€” Special Drop Today",
                     subtitle="SAMPLE â€” save this one")
        case "no_plays":
            c.update(note="SAMPLE â€” Nothing qualifies today. Discipline is a bet too.")
        case "story":
            c.update(hook="SAMPLE â€” 3 Bets I'm Taking", matchup="KC @ CIN",
                     bet_label="KC -1.5", odds="-115", conf_display="8/10")
    return c


tab_preview, tab_captions, tab_edit = st.tabs(["Card previews", "Caption previews", "Edit brand/hooks"])

# --------------------------------------------------------------- card preview
with tab_preview:
    templates = ["hook", "leg", "cta", "record", "recap_leg", "weekly", "cheatsheet",
                 "early_lines", "upset", "biggest_edge", "special", "no_plays", "story"]
    st.markdown("Rendering the real templates with fake data. Click refresh if a "
                "preview is missing (Playwright launch).")
    cols = st.columns(3)
    for i, t in enumerate(templates):
        out = PREVIEWS / f"sample_{t}.png"
        with cols[i % 3]:
            with st.spinner(f"rendering {t}â€¦"):
                try:
                    card_renderer.render_single(t, _ctx(t), out,
                                                size="story" if t == "story" else "feed")
                    st.image(str(out), caption=f"{t}.html", use_container_width=True)
                except Exception as exc:
                    st.error(f"{t}: {exc}")

# ------------------------------------------------------------- caption preview
with tab_captions:
    st.markdown("Sample captions from `templates/captions/*.jinja2` with SAMPLE data.")
    ctxs = {
        "picks": {"ctx": {"n_picks": 3, "slate_label": "Sunday Early", "top_conf": "8/10",
                          "avg_conf": "7/10", "combined_odds": "+350",
                          "players_display": "DET, KC, SF"},
                  "record": SAMPLE_RECORD},
        "recap": {"ctx": {"date": "2026-09-21"}, "record": SAMPLE_RECORD},
        "weekly": {"ctx": {"date": "2026-09-21", "record": SAMPLE_RECORD}, "record": SAMPLE_RECORD},
        "nfl_cheatsheet": {"ctx": {"date": "2026-09-21", "n_picks": 3}, "record": SAMPLE_RECORD},
    }
    for ep_type, extra in ctxs.items():
        opts = captions.caption_templates().get(ep_type, [])
        ctx = {"ep": SAMPLE_EP, "brand": BRAND, "disclaimer": DISCLAIMER,
               **extra["ctx"], "record": extra["record"]}
        for name in opts[:1]:
            st.markdown(f"**`{name}.jinja2`**")
            try:
                st.code(captions.render_caption(name, ctx), language="text")
            except Exception as exc:
                st.error(f"{name}: {exc}")

# ------------------------------------------------------------- edit yaml
with tab_edit:
    st.markdown("Edit `config/brand.yaml` and `templates/hooks.yaml` in place. "
                "Every change validates as YAML before saving.")
    brand_new = st.text_area("brand.yaml", value=BRAND_TEXT, height=420)
    hooks_new = st.text_area("hooks.yaml", value=HOOKS_TEXT, height=300)
    if st.button("Save both files", type="primary"):
        try:
            yaml.safe_load(brand_new)
            yaml.safe_load(hooks_new)
        except yaml.YAMLError as exc:
            st.error(f"Invalid YAML â€” nothing saved. {exc}")
        else:
            (ROOT / "config" / "brand.yaml").write_text(brand_new, encoding="utf-8")
            (ROOT / "templates" / "hooks.yaml").write_text(hooks_new, encoding="utf-8")
            st.success("Saved. Changes apply to the next generation.")
            st.rerun()