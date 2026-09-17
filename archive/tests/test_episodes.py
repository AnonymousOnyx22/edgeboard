"""Episode numbering: counters never repeat or skip; regenerating a draft
overwrites instead of consuming a new number."""
import os
import tempfile
import time

import pytest

from src.content import episode_manager as em
from src.db import database as db


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    db.init_db(path)
    return path


def _rm(path: str) -> None:
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.remove(path)


def test_numbers_monotonic_per_type():
    p = _tmp_db()
    try:
        a = em.create_or_regenerate("picks", "2026-09-01", db_path=p)
        b = em.create_or_regenerate("picks", "2026-09-02", db_path=p)
        c = em.create_or_regenerate("recap", "2026-09-01", db_path=p)
        assert a.number == 1
        assert b.number == 2
        assert c.number == 1  # separate counter per type
        d = em.create_or_regenerate("picks", "2026-09-03", db_path=p)
        assert d.number == 3  # no skips
    finally:
        _rm(p)


def test_no_repeat_for_same_type_and_date():
    p = _tmp_db()
    try:
        a = em.create_or_regenerate("picks", "2026-09-20", db_path=p)
        b = em.create_or_regenerate("picks", "2026-09-20", db_path=p)
        assert a.number == b.number == 1  # overwrite same draft, same number
        assert a.id == b.id
        c = em.create_or_regenerate("picks", "2026-09-21", db_path=p)
        assert c.number == 2
    finally:
        _rm(p)


def test_next_number_after_posted():
    p = _tmp_db()
    try:
        a = em.create_or_regenerate("weekly", "2026-09-13", db_path=p)
        em.set_status(a.id, "generated", p)
        em.set_status(a.id, "scheduled", p)
        em.set_status(a.id, "posted", p)
        b = em.create_or_regenerate("weekly", "2026-09-20", db_path=p)
        assert b.number == 2
    finally:
        _rm(p)


def test_status_transitions_guarded():
    p = _tmp_db()
    try:
        a = em.create_or_regenerate("picks", "2026-09-10", db_path=p)
        em.set_status(a.id, "generated", p)
        em.set_status(a.id, "draft", p)  # can go back to draft for edits
        em.set_status(a.id, "scheduled", p)
        em.set_status(a.id, "posted", p)
        with pytest.raises(ValueError):
            em.set_status(a.id, "scheduled", p)  # posted -> anything is blocked
    finally:
        _rm(p)