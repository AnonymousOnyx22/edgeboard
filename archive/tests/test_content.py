"""End-to-end content generation on a throwaway DB and output dir.

NOTE: uses clearly labeled SAMPLE data only, never stored in the real DB;
output pngs are written to a temp dir and removed.
"""
import os
import tempfile

import pytest

from src.content import captions, generator
from src.db import database as db


@pytest.fixture()
def fake_db(tmp_path):
    dbfile = str(tmp_path / "sample.sqlite")
    db.init_db(dbfile)  # schema seeds sports ('nfl' = id 1)
    # sample teams + one sample game (clearly labeled SAMPLE_* keys)
    with db.connect(dbfile) as conn:
        conn.execute("INSERT INTO teams (sport_id,league_key,name,abbrev) VALUES (1,'SAMPLE_A','Los Angeles Sample A','SAMP_A')")
        conn.execute("INSERT INTO teams (sport_id,league_key,name,abbrev) VALUES (1,'SAMPLE_B','Boston Sample B','SAMP_B')")
        conn.execute("""INSERT INTO games (sport_id, external_id, away_team, home_team, start_utc, slate, status)
                        VALUES (1, 'SAMPLE_GAME', 1, 2, '2026-09-20T17:00:00Z', 'sunday_early', 'scheduled')""")
        conn.commit()
    return dbfile


def _seed_pick(dbfile, source="model", status="approved", conf=8):
    now = "2026-09-19T00:00:00Z"
    with db.connect(dbfile) as conn:
        cur = conn.execute(
            """INSERT INTO picks (sport_id,nature,source,status,edge,confidence_model,
                                  confidence_final,units_model,units_final,reason,created_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (1, "straight", source, status, 0.04, conf, conf, 1, 1, "Sample reason in my voice.", now))
        pid = cur.lastrowid
        conn.execute("""INSERT INTO pick_legs (pick_id,ord,game_id,market,team,line,odds_american,
                       model_prob,implied_prob,edge) VALUES (?,0,1,'spread','SAMP_A',-3.0,-110,0.57,0.52,0.05)""", (pid,))
        conn.commit()
        return pid


def test_analysis_ctx_computes_odds_and_conf():
    picks = [{"team": "A", "market": "ml", "odds": -110, "confidence_model": 8}]
    ctx = captions.analysis_ctx(picks)
    assert ctx["n_picks"] == 1
    assert ctx["top_conf"] == "8/10"
    assert ctx["combined_odds"] == "-110"
    assert ctx["plays"][0]["odds"] == "-110"


def test_record_ctx_only_from_db(fake_db):
    # no graded picks yet -> empty record, no crash
    r = captions.record_ctx(fake_db)
    assert r["wins"] == 0 and r["losses"] == 0
    # store a graded result and see it reflected
    pid = _seed_pick(fake_db)
    with db.connect(fake_db) as conn:
        conn.execute("UPDATE picks SET status='graded' WHERE id=?", (pid,))
        conn.execute("INSERT INTO results (pick_id,result,units_won,graded_utc) VALUES (?,?,?,?)",
                     (pid, "W", 0.91, "2026-09-21T01:00:00Z"))
        conn.commit()
    r2 = captions.record_ctx(fake_db)
    assert r2["wins"] == 1 and r2["units"] == "+0.9"


def test_recap_pick_loader_includes_real_result(fake_db):
    pid = _seed_pick(fake_db)
    with db.connect(fake_db) as conn:
        conn.execute("UPDATE picks SET status='graded' WHERE id=?", (pid,))
        conn.execute("INSERT INTO results (pick_id,result,units_won,graded_utc) VALUES (?,?,?,?)",
                     (pid, "L", -1.0, "2026-09-21T01:00:00Z"))
        conn.commit()
    loaded = generator._load_picks([pid], fake_db)
    assert loaded[0]["result"] == "L"


def test_validate_caption_rejects_banned_words():
    brand = {"banned_words": ["guaranteed", "lock", "free money"]}
    captions.validate_caption("I like the under today", brand)  # ok
    with pytest.raises(captions.BannedWordError):
        captions.validate_caption("This is a guaranteed W", brand)


def test_generate_picks_episode_outputs_files(fake_db, tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    pid = _seed_pick(fake_db, status="approved", conf=8)
    result = generator.generate_episode("picks", "2026-09-20", [pid], sport="nfl",
                                        db_path=fake_db)
    out = tmp_path / "output" / "2026-09-20_picks_001"
    names = sorted(p.name for p in out.glob("*.png"))
    assert "01_hook.png" in names
    assert any(n.startswith("0") and "leg1" in n for n in names)
    assert (out / "story.png").exists()
    assert (out / "caption.txt").exists()
    caption = (out / "caption.txt").read_text(encoding="utf-8")
    assert "19+" in caption and "disclaimer" not in caption.lower().split() and "ConnexOntario" in caption
    assert (out / "hashtags.txt").read_text().strip()
    assert (out / "alt_text.txt").exists()
    assert (out / "episode.json").exists()
    # episode row updated
    ep = generator.em.get_episode(result["id"], db_path=fake_db)
    assert ep.status == "generated"
    assert ep.number == 1
    edited = caption + "\n\nEdited after review."
    generator.save_caption(ep.id, edited, db_path=fake_db)
    assert (out / "caption.txt").read_text(encoding="utf-8") == edited
    assert generator.em.get_episode(ep.id, db_path=fake_db).caption_text == edited
    # regenerate only overwrites a DRAFT (no new number); a generated episode gets a new one
    generator.em.set_status(ep.id, "draft", db_path=fake_db)
    r2 = generator.generate_episode("picks", "2026-09-20", [pid], sport="nfl", db_path=fake_db)
    assert r2["number"] == 1
    r3 = generator.generate_episode("picks", "2026-09-20", [pid], sport="nfl", db_path=fake_db)
    assert r3["number"] == 2


def test_generate_no_plays_does_not_require_a_pick(fake_db, tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    result = generator.generate_episode("no_plays", "2026-09-20", [], sport="nfl",
                                        note="No qualified edge today.", db_path=fake_db)
    out = tmp_path / "output" / "2026-09-20_no_plays_001"
    assert result["n_slides"] == 1
    assert (out / "01_no_plays.png").exists()
    assert "19+" in (out / "caption.txt").read_text(encoding="utf-8")
