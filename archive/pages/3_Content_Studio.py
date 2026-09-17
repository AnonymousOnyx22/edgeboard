"""Content Studio: generate episodes from approved picks, preview slides +
caption, edit, mark scheduled/posted, download a Buffer-ready ZIP."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import streamlit as st

from src.app.common import render_sidebar
from src.content import captions, episode_manager as em, generator
from src.db import database as db

ROOT = Path(__file__).resolve().parents[3]

st.set_page_config(page_title="Content Studio", layout="wide")
render_sidebar()
st.subheader("Content Studio")

EP_TYPES = [
    ("picks", "Daily Picks (carousel)"),
    ("recap", "Recap (results)"),
    ("weekly", "Weekly Report"),
    ("nfl_cheatsheet", "Sunday Cheat Sheet"),
    ("upset_alert", "Upset Alert"),
    ("early_lines", "Early Lines"),
    ("biggest_edge", "Biggest Edge"),
    ("no_plays", "No Plays / rest day"),
    ("special", "Special"),
]
GEN = {"picks", "recap", "upset_alert", "biggest_edge", "no_plays"}
REQUIRES_PICKS = {"picks", "recap", "nfl_cheatsheet", "upset_alert",
                  "early_lines", "biggest_edge"}


def _approved_picks() -> list[dict]:
    return [dict(r) for r in db.query(db.DEFAULT_DB, """
        SELECT p.id, p.sport_id, p.confidence_final, p.units_final,
               l.team, l.market, l.line, l.odds_american AS odds, l.reason,
               g.start_utc, ht.name || ' @ ' || at.name AS matchup, g.slate
        FROM picks p
        JOIN pick_legs l ON l.pick_id = p.id AND l.ord = 0
        JOIN games g ON g.id = l.game_id
        JOIN teams ht ON ht.id = g.home_team
        JOIN teams at ON at.id = g.away_team
        WHERE p.status IN ('approved','edited') AND p.episode_id IS NULL
        ORDER BY p.confidence_final DESC""")]


def _graded_picks() -> list[dict]:
    return [dict(r) for r in db.query(db.DEFAULT_DB, """
        SELECT p.id, p.sport_id, p.confidence_final, p.units_final,
               l.team, l.market, l.line, l.odds_american AS odds, l.reason,
               g.start_utc, ht.name || ' @ ' || at.name AS matchup, g.slate,
               r.result, r.units_won
        FROM picks p
        JOIN pick_legs l ON l.pick_id = p.id AND l.ord = 0
        JOIN games g ON g.id = l.game_id
        JOIN teams ht ON ht.id = g.home_team
        JOIN teams at ON at.id = g.away_team
        JOIN results r ON r.pick_id = p.id
        ORDER BY r.graded_utc DESC LIMIT 30""")]


def _hook_keys(ep_type: str) -> list[str]:
    return captions.load_hooks().get(ep_type, [])


def _episode_label(e: em.Episode) -> str:
    return f"#{e.number} Â· {e.ep_type} Â· {e.date} Â· {e.status}"


gen, review = st.tabs(["Generate", "Review & Edit"])

# ------------------------------------------------------------------ generate
with gen:
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        st.markdown("**1. Type**")
        ep_type = st.selectbox("Episode type", EP_TYPES, format_func=lambda t: t[1])[0]
        ep_date = st.text_input("Episode date (YYYY-MM-DD)", value=dt.date.today().isoformat())
        hooks = ["Auto (rotate, no repeats)"] + _hook_keys(ep_type)
        hook_sel = st.selectbox("Hook", hooks)
        hook = None if hook_sel.startswith("Auto") else hook_sel
        note = st.text_input("Note (blank = auto)", value="")

        st.markdown("**2. Picks**")
        if ep_type == "recap":
            pool = _graded_picks()
            empty = "No graded picks yet â€” grade results in Results & Record first."
        else:
            pool = _approved_picks()
            empty = "No approved picks waiting. Approve suggestions in Today first."
        if not pool:
            st.warning(empty)
            pick_ids = []
        else:
            pick_ids = st.multiselect(
                "Select picks",
                [p["id"] for p in pool],
                format_func=lambda i: f"{next(x for x in pool if x['id']==i)['matchup']} "
                                      f"| {next(x for x in pool if x['id']==i)['team']} "
                                      f"{next(x for x in pool if x['id']==i)['market'].upper()} "
                                      f"({next(x for x in pool if x['id']==i)['odds']}) "
                                      f"Conf {next(x for x in pool if x['id']==i).get('confidence_final') or '-'}",
                help=empty)
        if st.button("Generate episode", type="primary"):
            if not pick_ids and ep_type in REQUIRES_PICKS:
                st.error("Select at least one pick.")
            else:
                try:
                    res = generator.generate_episode(ep_type, ep_date, pick_ids,
                                                     sport="nfl", hook=hook, note=note,
                                                     db_path=db.DEFAULT_DB)
                    st.success(f"Episode #{res['number']} generated â†’ {res['output_dir']}")
                except Exception as exc:
                    st.error(f"Generation failed: {exc}")

    with c2:
        st.markdown("**Slate today (NFL)**")
        games = db.query(db.DEFAULT_DB, """
            SELECT slate, COUNT(*) n FROM games
            WHERE COALESCE(game_date, date(start_utc))=date('now','localtime')
              AND status != 'final'
            GROUP BY slate""")
        for g in games or []:
            st.caption(f"{g['slate']}: {g['n']} games")
        st.markdown("**How numbering works**")
        st.caption("Each type has its own counter. A draft for the same type+date "
                   "is overwritten instead of taking a new number; once it's "
                   "generated, the next one gets a fresh number.")

# ------------------------------------------------------------------- review
with review:
    eps = em.list_episodes(db_path=db.DEFAULT_DB)
    eps = [e for e in eps if e.status in ("generated", "scheduled", "posted", "draft")]
    if not eps:
        st.info("Nothing generated yet. Use the Generate tab.")
    else:
        sel = st.selectbox("Episode", eps, format_func=lambda e: _episode_label(e))
        if sel:
            out_dir = Path(sel.output_dir or "")
            slides = sorted(out_dir.glob("*.png")) if out_dir.exists() else []
            if slides:
                cols = st.columns(min(len(slides), 3))
                for i, img in enumerate(slides):
                    with cols[i % 3]:
                        st.image(str(img), caption=img.name, use_container_width=True)
            st.write(f"**Status:** {sel.status}  Â·  **Picks:** {len(sel.linked_pick_ids)}")

            caption = st.text_area("Caption (edit â†’ Save)", value=sel.caption_text or "",
                                   height=180)
            if st.button("Save caption"):
                try:
                    generator.save_caption(sel.id, caption, db_path=db.DEFAULT_DB)
                except Exception as exc:
                    st.error(f"Caption was not saved: {exc}")
                else:
                    st.success("Caption and download pack updated.")

            colA, colB, colC = st.columns(3)
            if colA.button("Mark scheduled", type="primary",
                           disabled=sel.status not in ("generated", "draft")):
                em.set_status(sel.id, "scheduled", db_path=db.DEFAULT_DB)
                st.rerun()
            if colB.button("Mark posted", disabled=sel.status not in ("scheduled", "generated")):
                em.set_status(sel.id, "posted", db_path=db.DEFAULT_DB)
                st.rerun()
            if colC.button("Back to draft", disabled=sel.status in ("posted",)):
                em.set_status(sel.id, "draft", db_path=db.DEFAULT_DB)
                st.rerun()

            if out_dir.exists():
                zpath = out_dir.with_suffix(".zip")
                if not zpath.exists():
                    try:
                        from src.content.buffer_pack import BufferPack
                        BufferPack({}).zip(out_dir)
                    except Exception:
                        pass
                if zpath.exists():
                    st.download_button("Download Buffer ZIP",
                                       data=open(zpath, "rb").read(),
                                       file_name=zpath.name, mime="application/zip")
