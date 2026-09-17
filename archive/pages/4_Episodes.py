"""Episodes: list, filter, status. Create with Content Studio."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src.app.common import render_sidebar
from src.content import episode_manager as em
from src.db import database as db

st.set_page_config(page_title="Episodes", layout="wide")
render_sidebar()

f1, f2, f3 = st.columns(3)
ep_types = [r["value"] for r in db.query(db.DEFAULT_DB,
             "SELECT DISTINCT ep_type AS value FROM episodes ORDER BY ep_type")]
ep_type = f1.selectbox("Type", ["All"] + list(ep_types) if ep_types else ["All"])
sport_filter = f2.selectbox("Sport", ["All", "nfl", "mlb", "nhl", "nba"])
status_filter = f3.selectbox("Status", ["All", "draft", "generated", "scheduled", "posted"])

eps = em.list_episodes(db_path=db.DEFAULT_DB)
if ep_type != "All":
    eps = [e for e in eps if e.ep_type == ep_type]
if sport_filter != "All":
    eps = [e for e in eps if e.sport == sport_filter]
if status_filter != "All":
    eps = [e for e in eps if e.status == status_filter]

if not eps:
    st.info("No episodes yet. Generate your first one in Content Studio (phase C).")
else:
    df = pd.DataFrame([{
        "id": e.id, "number": e.number, "type": e.ep_type, "date": e.date,
        "slate": e.slate or "", "sport": e.sport or "", "status": e.status,
        "template": e.template_key or "", "picks": len(e.linked_pick_ids),
    } for e in eps])
    st.dataframe(df, use_container_width=True, hide_index=True)
    sel = st.selectbox("Episode", df["id"].tolist(),
                       format_func=lambda i: f"#{int(i)}")
    if sel:
        ep = em.get_episode(int(sel), db_path=db.DEFAULT_DB)
        st.write(f"**Episode #{ep.number} ({ep.ep_type})** â€” {ep.date} â€” {ep.status}")
        if ep.caption_text:
            st.code(ep.caption_text, language="text")
        if ep.linked_pick_ids:
            st.caption(f"Linked picks: {ep.linked_pick_ids}")