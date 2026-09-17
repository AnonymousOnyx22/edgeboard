# Archived content-generation code

Moved out of the active app on 2026-09-17 when the dashboard was refocused into a
parlay picker. Nothing here is imported by the running app.

| Folder | What it was |
|---|---|
| `content/` | Episode generator, caption builder, card renderer (Playwright), Buffer pack export |
| `instagram/` | Instagram Graph API read-only insights client |
| `templates/` | Jinja2 caption templates and HTML slide cards |
| `pages/` | Streamlit pages: Content Studio, Episodes, Weekly Calendar, Templates, Growth |
| `tests/` | Tests for the above |

## Restoring it

```bash
mv archive/content src/content
mv archive/instagram src/instagram
mv archive/templates templates
mv archive/pages/*.py src/app/pages/
mv archive/tests/*.py tests/
```

The DB tables it used (`episodes`, `episode_picks`, `counters`, `ig_snapshots`,
`ig_posts`) were left in `src/db/schema.sql` on purpose, so archived code still
matches the database when you bring it back. The `episodes` / `recap` / `weekly` /
`regen` / `sync-instagram` CLI commands were removed from `main.py` and will need
re-adding.
