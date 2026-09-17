"""Growth & Instagram (phase D): link account, pull read-only analytics."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from src.app.common import render_sidebar
from src.db import database as db
from src.instagram import graph_api as ig

st.set_page_config(page_title="Growth & Instagram", layout="wide")
render_sidebar()
st.subheader("Instagram growth analytics")

if not ig.token_configured():
    st.warning("No token configured yet.")
    st.markdown("""
**Link in ~10 minutes (free Meta Developer app, read-only):**
1. Instagram account â†’ Settings â†’ Account type â†’ switch to **Professional** (Creator or Business).
2. In the Instagram app â†’ Edit profile â†’ Page â†’ **link a Facebook Page** you manage.
3. Go to [developers.facebook.com](https://developers.facebook.com) â†’ **My Apps â†’ Create App** (type: Business) â†’ add the **Instagram product**.
4. Add permissions: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`.
5. Use the Graph API Explorer to get a **short-lived token**, then exchange it for a long-lived token (60 days, free) â€” or call the `GET /access_token` exchange.
6. Put it in a local `.env`: `IG_LONG_TOKEN=...` (never shared, never committed).
7. Restart the dashboard and click **Fetch now**.
""")
    st.stop()

st.success(f"Configured: connector token {ig.mask_token()}")
col1, col2, col3 = st.columns(3)

if st.button("Fetch now", type="primary"):
    try:
        res = ig.InstagramClient().pull_all(db_path=db.DEFAULT_DB)
        st.success(f"Synced @{res['username']}: {res['followers']} followers, "
                   f"{res['media_synced']} posts pulled.")
    except ig.IGError as exc:
        st.error(str(exc))

snaps = [dict(r) for r in db.query(db.DEFAULT_DB,
                                    "SELECT * FROM ig_snapshots ORDER BY snapshot_date")]
posts = [dict(r) for r in db.query(db.DEFAULT_DB, """
        SELECT * FROM ig_posts WHERE timestamp != '' ORDER BY timestamp DESC LIMIT 200""")]

if not snaps and not posts:
    st.info("No data yet â€” configure a token and press **Fetch now**.")
    st.stop()

if snaps:
    df_s = pd.DataFrame(snaps)
    st.markdown("#### Follower growth")
    st.plotly_chart(px.line(df_s, x="snapshot_date", y="followers"),
                    use_container_width=True)

if posts:
    df_p = pd.DataFrame(posts)
    df_p["day"] = pd.to_datetime(df_p["timestamp"]).dt.strftime("%a")
    for k, label in [("reach", "Reach"), ("saves", "Saves"), ("shares", "Shares"), ("likes", "Likes")]:
        if k in df_p.columns:
            st.markdown(f"#### Top posts by {label}")
            top = df_p.dropna(subset=[k]).sort_values(k, ascending=False).head(5)
            st.dataframe(top[["timestamp", "media_type", "caption", k]], use_container_width=True,
                         hide_index=True)
    if "reach" in df_p.columns and df_p["reach"].notna().any():
        st.markdown("#### Engagement by weekday")
        daily = df_p.dropna(subset=["reach"]).groupby("day")["reach"].mean().reindex(
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        st.bar_chart(daily)

st.caption("All data read-only. Nothing is posted from this tool.")