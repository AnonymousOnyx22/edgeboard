"""HTML/CSS -> PNG card renderer using Playwright (headless Chromium, free).

- 1080x1350 feed carousel slides, 1080x1920 stories, dpr=2 for sharp text.
- Templates live in templates/cards/*.html, styled by templates/cards/_slide_head.html.
- Requires: python -m playwright install chromium  (once, free).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import jinja2

CARDS_DIR = Path(__file__).resolve().parents[2] / "templates" / "cards"

_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(CARDS_DIR)),
                          autoescape=False, trim_blocks=True, lstrip_blocks=True)

_sizes = {
    "feed": (1080, 1350),
    "story": (1080, 1920),
}


class Renderer:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        return self._browser

    async def render_one(self, template_name: str, context: dict, out_path: Path,
                         size: str = "feed") -> Path:
        html = _env.get_template(f"{template_name}.html").render(**context)
        browser = await self._ensure_browser()
        page = await browser.new_page(viewport={"width": _sizes[size][0], "height": _sizes[size][1]},
                                      device_scale_factor=2)
        await page.set_content(html, wait_until="load", timeout=15_000)
        await page.wait_for_timeout(600)  # let webfonts settle
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out_path), full_page=False)
        await page.close()
        return out_path

    async def render_many(self, jobs: list[tuple[str, dict, Path, str]]) -> list[Path]:
        """jobs: [(template, ctx, out_path, size)] - one browser, parallel pages."""
        browser = await self._ensure_browser()
        async def one(job):
            tmpl, ctx, out, size = job
            html = _env.get_template(f"{tmpl}.html").render(**ctx)
            page = await browser.new_page(viewport={"width": _sizes[size][0], "height": _sizes[size][1]},
                                          device_scale_factor=2)
            try:
                await page.set_content(html, wait_until="load", timeout=15_000)
                await page.wait_for_timeout(600)
                out.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(out), full_page=False)
            finally:
                await page.close()
            return out
        return await asyncio.gather(*[one(j) for j in jobs])

    async def close(self):
        if self._browser:
            await self._browser.close()
            await self._pw.stop()
            self._browser = None


def render_slides(jobs: list[tuple[str, dict, Path, str]]) -> list[Path]:
    """Synchronous convenience wrapper around Renderer.render_many."""
    async def _run():
        r = Renderer()
        try:
            return await r.render_many(jobs)
        finally:
            await r.close()
    return asyncio.run(_run())


def render_single(template_name: str, context: dict, out_path: Path,
                  size: str = "feed") -> Path:
    return render_slides([(template_name, context, out_path, size)])[0]