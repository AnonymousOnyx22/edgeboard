"""Instagram Graph API client. Read-only, 100% free (no posting automation).

Endpoints used:
  GET /me                                  -> id, username, followers_count, media_count, ...
  GET /me/media?fields=...,&limit=100      -> recent media
  GET /{media}/insights?metric=reach,saved,shares  -> engagement insights

Requires a long-lived IG token in .env as IG_LONG_TOKEN (see README).
All failures are non-fatal: callers snap into None/[].
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from ..db import database as db

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

API_BASE = "https://graph.instagram.com/v21.0"
CACHE_AGE = 6 * 3600  # seconds


class IGError(RuntimeError):
    pass


def token_configured() -> bool:
    return bool(os.getenv("IG_LONG_TOKEN"))


def mask_token() -> str:
    t = os.getenv("IG_LONG_TOKEN", "")
    return f"{t[:6]}…{t[-4:]}" if len(t) > 12 else "(not set)"


class InstagramClient:
    def __init__(self, token: str | None = None, cache: Path | None = None):
        self.token = token or os.getenv("IG_LONG_TOKEN")
        self.cache = cache or ROOT / "data" / "ig_cache.json"
        if not self.token:
            raise IGError("IG_LONG_TOKEN not set in .env")

    # ------------------------------------------------------------- internals
    def _get(self, path: str, params: dict | None = None, ttl: int = CACHE_AGE) -> dict | None:
        """GET with a short local JSON cache (per url+params)."""
        key = path + str(sorted((params or {}).items()))
        if self.cache.exists():
            try:
                blob = json.loads(self.cache.read_text(encoding="utf-8"))
            except Exception:
                blob = {}
        else:
            blob = {}
        hit = blob.get(key)
        if hit and hit.get("ts", 0) > dt.datetime.now().timestamp() - ttl:
            return hit.get("data")
        params = dict(params or {"access_token": self.token})
        params["access_token"] = self.token
        resp = requests.get(f"{API_BASE}/{path}", params=params, timeout=30)
        if resp.status_code != 200:
            raise IGError(f"IG API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        blob[key] = {"ts": dt.datetime.now().timestamp(), "data": data}
        self.cache.write_text(json.dumps(blob), encoding="utf-8")
        return data

    # ------------------------------------------------------------------ data
    def me(self) -> dict:
        data = self._get("me", {"fields": "id,username,account_type,media_count,"
                                         "followers_count,follows_count"})
        if not data:
            raise IGError("empty /me response")
        return data

    def media(self, limit: int = 100) -> list[dict]:
        out, after = [], None
        while True:
            params = {"fields": "id,caption,timestamp,media_type,like_count,comments_count",
                      "limit": min(limit - len(out), 50)}
            if after:
                params["after"] = after
            data = self._get("me/media", params)
            items = data.get("data", []) if data else []
            out.extend(items)
            after = (data or {}).get("paging", {}).get("cursors", {}).get("after")
            if not items or len(out) >= limit or not after:
                break
        return out

    def media_insights(self, media_id: str) -> dict:
        data = self._get(f"{media_id}/insights",
                         {"metric": "reach,saved,shares"})
        if not data:
            return {}
        items = data.get("data", []) if data else []
        return {m["name"]: m["values"][0]["value"] for m in items
                if m.get("values")}

    # -------------------------------------------------------------- snapshot
    def pull_all(self, db_path=db.DEFAULT_DB) -> dict:
        """Fetch profile + recent media + insights, upsert into DB."""
        me = self.me()
        today = dt.date.today().isoformat()
        db.execute(db_path, """INSERT INTO ig_snapshots
                      (snapshot_date, followers, follows, media_count, pulled_utc)
                      VALUES (?,?,?,?,?)
                      ON CONFLICT(snapshot_date) DO UPDATE SET
                        followers=excluded.followers, follows=excluded.follows,
                        media_count=excluded.media_count, pulled_utc=excluded.pulled_utc""",
                   (today, me.get("followers_count"), me.get("follows_count"),
                    me.get("media_count"), db.utc_now()))
        posts = []
        media = self.media(limit=50)
        for m in media:
            ins = {}
            if m.get("media_type") in ("IMAGE", "CAROUSEL_ALBUM"):
                try:
                    ins = self.media_insights(m["id"])
                except IGError:
                    ins = {}
            db.execute(db_path, """INSERT INTO ig_posts
                        (ig_id, timestamp, media_type, caption, likes, comments,
                         saves, shares, reach, pulled_utc)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(ig_id) DO UPDATE SET
                          caption=excluded.caption, likes=excluded.likes,
                          comments=excluded.comments, saves=excluded.saves,
                          shares=excluded.shares, reach=excluded.reach,
                          pulled_utc=excluded.pulled_utc""",
                       (m["id"], m.get("timestamp"), m.get("media_type"),
                        (m.get("caption") or "")[:4000], m.get("like_count"),
                        m.get("comments_count"), ins.get("saved"),
                        ins.get("shares"), ins.get("reach"), db.utc_now()))
            posts.append(m["id"])
        return {"username": me.get("username"), "followers": me.get("followers_count"),
                "media_synced": len(posts), "date": today}