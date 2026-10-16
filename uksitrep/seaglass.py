"""Sea glass hunting forecast from seaglasshunting.com (their ShoreSense score).

There is no API, so this reads two public pages, and only when ENABLE_SEAGLASS=true:
- the weekly leaderboard page: the Top 25 is plain HTML (`<ol class="tide-fragment-list">`), fetched every 3 hours;
- the site's /map/ page, whose schema.org JSON-LD gives every beach's position, fetched weekly and kept in DATA_DIR.

Mixed into Situation, so this uses its session, cache and helpers. Only the beaches on the leaderboard are
shown, linked back to the site, with the site credited. If the page layout changes the parser raises and
viewers keep the last good copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import html
import json
import logging
import re
import time

_LOGGER = logging.getLogger("uksitrep")

BASE = "https://seaglasshunting.com"
BOARD_URL = BASE + "/this-weeks-best-sea-glass-beaches/"
MAP_URL = BASE + "/map/"
BOARD_TTL = 3 * 3600
BEACH_REFRESH_S = 7 * 86400
UK_BOX = (49.0, -9.0, 61.5, 2.5)   # S, W, N, E: positions outside are ignored
MIN_ROWS = 10    # fewer parsed rows than this means the page changed, not that the sea went flat

_LI = re.compile(r'<li value="(\d+)">(.*?)</li>', re.S)
_LINK = re.compile(r'<a href="https://seaglasshunting\.com/beach/([a-z0-9-]{2,60})"[^>]*>([^<]+)</a>')
_SCORE = re.compile(r"<strong>(\d{1,3})</strong>\s*/\s*100")
_LABEL = re.compile(r"<span>\(([^)<]{1,40})\)</span>")


def _span(row: str, cls: str) -> str | None:
    m = re.search(rf'<span class="tide-fragment-{cls}">(.*?)</span>', row, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()[:120] or None if m else None


def parse_board(page: str) -> dict:
    """The weekly Top 25 -> {"updated": "YYYY-MM-DD" or None, "rows": [...]}."""
    i = page.find('id="tide-leaderboard"')
    if i < 0:
        raise ValueError("sea glass leaderboard not found in the page")
    frag = page[i:page.find("</section>", i)]
    rows = []
    for rank, body in _LI.findall(frag):
        link, score = _LINK.search(body), _SCORE.search(body)
        if not link or not score or int(score.group(1)) > 100:
            continue
        label = _LABEL.search(body)
        when = _span(body, "when")
        rows.append({
            "rank": int(rank), "slug": link.group(1), "name": html.unescape(link.group(2)).strip(),
            "region": _span(body, "region"), "score": int(score.group(1)), "label": label.group(1) if label else None,
            "sea": _span(body, "sea"), "note": _span(body, "extra"),
            "window": re.sub(r"^best window\s*", "", when) if when else None,
        })
    day = re.search(r"updated (\d{4}-\d{2}-\d{2})", frag)
    return {"updated": day.group(1) if day else None, "rows": rows}


def parse_beaches(page: str) -> dict[str, dict]:
    """The /map/ page's JSON-LD places -> {slug: {name, region, lat, lon}}."""
    out: dict[str, dict] = {}

    def walk(o) -> None:
        if isinstance(o, dict):
            geo, url = o.get("geo"), str(o.get("url") or "")
            m = re.search(r"/beach/([a-z0-9-]{2,60})/?$", url)
            if m and isinstance(geo, dict):
                try:
                    lat, lon = float(geo["latitude"]), float(geo["longitude"])
                except (KeyError, TypeError, ValueError):
                    lat = lon = None
                if lat is not None and UK_BOX[0] <= lat <= UK_BOX[2] and UK_BOX[1] <= lon <= UK_BOX[3]:
                    addr = o.get("address")
                    out[m.group(1)] = {"name": html.unescape(str(o.get("name") or "")), "lat": round(lat, 5),
                                       "lon": round(lon, 5), "region": addr if isinstance(addr, str) else None}
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return out


class SeaGlass:
    """Mixin for Situation."""

    _sg_beaches: dict[str, dict] | None = None   # slug -> position, loaded from DATA_DIR or the /map/ page
    _sg_beaches_at = 0.0
    _sg_retry_at = 0.0

    async def _sg_positions(self) -> dict[str, dict]:
        store = self.cfg.data_dir / "seaglass_beaches.json"
        if self._sg_beaches is None:
            try:
                saved = await asyncio.to_thread(lambda: json.loads(store.read_text()))
                self._sg_beaches, self._sg_beaches_at = saved["beaches"], float(saved.get("fetched", 0))
            except (OSError, ValueError, KeyError, TypeError):
                self._sg_beaches = {}
        if time.time() - self._sg_beaches_at > BEACH_REFRESH_S and time.time() > self._sg_retry_at:
            try:
                found = parse_beaches(await self._text(MAP_URL))
                if len(found) < 20:
                    raise ValueError(f"only {len(found)} beaches found on the map page")
                now = time.time()
                await asyncio.to_thread(lambda: store.write_text(json.dumps({"fetched": now, "beaches": found}, separators=(",", ":"))))
                self._sg_beaches, self._sg_beaches_at = found, now
                _LOGGER.info("sea glass: %d beach positions", len(found))
            except Exception as err:  # noqa: BLE001 - keep the positions we have
                self._sg_retry_at = time.time() + 3600
                _LOGGER.warning("sea glass: beach positions not refreshed (%s)", err)
        return self._sg_beaches

    async def seaglass(self) -> dict:
        if not self.cfg.enable_seaglass:
            return {"error": "disabled: set ENABLE_SEAGLASS=true (reads seaglasshunting.com)"}

        async def fetch() -> dict:
            board = parse_board(await self._text(BOARD_URL))
            if len(board["rows"]) < MIN_ROWS:
                raise ValueError(f"sea glass leaderboard: only {len(board['rows'])} rows parsed, has the page changed?")
            positions = await self._sg_positions()
            beaches = []
            for r in board["rows"]:
                p = positions.get(r["slug"])
                if p:
                    beaches.append({**r, "lat": p["lat"], "lon": p["lon"], "url": f"{BASE}/beach/{r['slug']}"})
            if not beaches:
                raise ValueError("sea glass: none of the leaderboard beaches has a position")
            return {"updated": datetime.now(timezone.utc).isoformat(), "source_updated": board["updated"],
                    "beaches": beaches, "source": BOARD_URL}

        return await self.cache.get("seaglass", BOARD_TTL, fetch)
