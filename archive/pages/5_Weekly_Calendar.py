"""Weekly Calendar: 7-day posting plan with real episode status counts."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src.app.common import render_sidebar
from src.content import episode_manager as em
from src.db import database as db

st.set_page_config(page_title="Weekly Calendar", layout="wide")
render_sidebar()
st.subheader("Weekly Calendar")

PLAN = [
    ("Monday", "Sunday recap", "recap"),
    ("Tuesday", "Weekly report", "weekly"),
    ("Wednesday", "Early lines watch", "early_lines"),
    ("Thursday", "TNF picks", "picks"),
    ("Friday", "Upset alert / biggest edge", "biggest_edge"),
    ("Saturday", "Sunday cheat sheet", "nfl_cheatsheet"),
    ("Sunday", "Main slate + openers", "picks"),
]

today = dt.date.today()
rows = []
for offset, (day, label, ep_type) in enumerate(PLAN):
    d = today + dt.timedelta(days=offset)
    eps = em.list_episodes(ep_type=ep_type, date_from=d.isoformat(), db_path=db.DEFAULT_DB)
    status = next((e.status for e in eps if e.date == d.isoformat()), "â€”")
    ok = "âœ…" if status in ("scheduled", "posted", "generated") else "â¬œ"
    rows.append({
        "Day": day, "Date": d.isoformat(), "Planned": label,
        "Episode type": ep_type, "Status": status, "": ok,
    })

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

eps7 = em.list_episodes(date_from=today.isoformat(), db_path=db.DEFAULT_DB)
eps7 = [e for e in eps7 if e.date and e.date <= (today + dt.timedelta(days=6)).isoformat()]
if eps7:
    st.markdown("**Next 7 days in the system**")
    st.dataframe(pd.DataFrame([{
        "date": e.date, "type": e.ep_type, "n": e.number, "status": e.status,
        "picks": len(e.linked_pick_ids),
    } for e in sorted(eps7, key=lambda e: (e.date, e.ep_type))]),
        use_container_width=True, hide_index=True)

st.markdown("""
**Looking ahead:** when MLB/NHL/NBA are enabled in Settings, the plan row for a
day where no games are scheduled will switch to `no_plays` (rest day) and the
Other Sports picks render on their own slates.
""")