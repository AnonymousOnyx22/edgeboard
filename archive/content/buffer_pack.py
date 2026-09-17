"""Packing rendered slides into a Buffer-ready episode folder.

output/YYYY-MM-DD_<type>_<NNN>/
    01_hook.png 02_leg1.png ... 06_cta.png
    story.png
    reel/  (1080x1920 story-sized slides)
    caption.txt, hashtags.txt, alt_text.txt, episode.json
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from . import card_renderer

FEED_NAMES = {
    "hook": "hook", "record": "record", "cta": "cta",
    "recap_leg": "recap", "weekly": "weekly", "cheatsheet": "cheatsheet",
    "upset": "upset", "early_lines": "early_lines", "biggest_edge": "biggest_edge",
    "special": "special", "no_plays": "no_plays",
}


def _name(tmpl: str, ctx: dict, idx: int) -> str:
    if tmpl == "leg":
        return f"leg{ctx.get('leg_num', idx)}"
    return FEED_NAMES.get(tmpl, tmpl)


class BufferPack:
    def __init__(self, brand: dict):
        self.brand = brand

    # ------------------------------------------------------------ feed
    def render(self, slides: list[tuple[str, dict]], output_dir: Path) -> list[Path]:
        jobs = []
        for idx, (tmpl, ctx) in enumerate(slides, 1):
            out = output_dir / f"{idx:02d}_{_name(tmpl, ctx, idx)}.png"
            jobs.append((tmpl, ctx, out, "feed"))
        return card_renderer.render_slides(jobs)

    # -------------------------------------------------- story + reel
    def story(self, ctx: dict, output_dir: Path) -> Path:
        """Single 1080x1920 story with poll-sticker space."""
        return card_renderer.render_single("story", ctx, output_dir / "story.png", size="story")

    def reel(self, slides: list[tuple[str, dict]], output_dir: Path) -> list[Path]:
        """1080x1920 story-sized slides for photo Reels."""
        reel_dir = output_dir / "reel"
        jobs = []
        for idx, (tmpl, ctx) in enumerate(slides, 1):
            jobs.append((tmpl, ctx, reel_dir / f"{idx:02d}_{_name(tmpl, ctx, idx)}.png", "story"))
        return card_renderer.render_slides(jobs)

    # ------------------------------------------------------- text
    def write_text(self, output_dir: Path, caption: str, hashtags: str, alt_text: str) -> None:
        (output_dir / "caption.txt").write_text(caption, encoding="utf-8")
        (output_dir / "hashtags.txt").write_text(hashtags, encoding="utf-8")
        (output_dir / "alt_text.txt").write_text(alt_text, encoding="utf-8")

    # ---------------------------------------------------------- zip
    def zip(self, output_dir: Path) -> Path:
        zpath = output_dir.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(output_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(output_dir.parent))
        return zpath

    # ---------------------------------------------------------- meta
    def write_episode_json(self, output_dir: Path, meta: dict) -> None:
        (output_dir / "episode.json").write_text(json.dumps(meta, indent=2))