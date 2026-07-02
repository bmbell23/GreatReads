"""Apple / iTunes Search API client (#130) — free, no API key.

Apple Books exposes book records through the public iTunes Search API. It's a
great high-resolution cover source: the search response gives a ``artworkUrl100``
(100×100) thumbnail whose URL embeds the requested size, so swapping the size
segment for a large value returns Apple's near-original artwork.

Used only as an extra *cover candidate* in the per-book metadata compare window,
so a wrong-edition hit is a choice the user rejects, not an auto-apply.

Polite usage: a descriptive User-Agent and a small inter-request delay.
"""

import re
from time import sleep
from typing import Optional

import requests

_UA = "GreatReads/1.0 (personal reading tracker; contact via app)"
_SEARCH = "https://itunes.apple.com/search"
# artworkUrl100 ends in ".../<W>x<H>bb.jpg" (or -999.jpg); swap the size segment for a
# large one to get Apple's max-res artwork. Apple serves up to the image's *native*
# resolution for any reasonable request but 400s on absurd sizes (100000+ fails,
# 3000-5000 returns native max) — so ask for 3000×3000.
_SIZE_RE = re.compile(r"/\d+x\d+((?:bb)?(?:-\d+)?\.(?:jpg|png))$")
_HIRES = "/3000x3000\\1"


def _upscale(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return _SIZE_RE.sub(_HIRES, url)


class ITunesClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    def _search_one(self, title: str, author: str) -> Optional[dict]:
        """Raw first iTunes ebook result for a title (+author), or None on miss."""
        term = " ".join(p for p in (title, author) if p).strip()
        if not term:
            return None
        try:
            r = self.session.get(_SEARCH, params={
                "term": term, "media": "ebook", "entity": "ebook", "limit": 1},
                timeout=10)
            sleep(0.2)
            if r.status_code != 200:
                return None
            results = r.json().get("results", [])
        except (requests.exceptions.RequestException, ValueError):
            return None
        return results[0] if results else None

    def cover_by_title_author(self, title: str, author: str) -> Optional[str]:
        """High-res Apple Books cover URL for a title (+author), or None on miss."""
        r = self._search_one(title, author)
        return _upscale(r.get("artworkUrl100")) if r else None

    def lookup(self, title: str, author: str) -> Optional[dict]:
        """Full Apple Books record as enrichment candidates (#158): cover, synopsis,
        primary genre, release date, and the average user rating (0–5). None on miss.

        iTunes ``description`` is HTML; callers strip it. ``averageUserRating`` is a
        community rating on Apple's own 0–5 scale — kept separate from the user's
        own ratings (books.public_rating)."""
        r = self._search_one(title, author)
        if not r:
            return None
        rating = r.get("averageUserRating")
        # ebook records carry a clean ``genres`` list (["Epic Fantasy","Books",
        # "Sci-Fi & Fantasy","Fantasy"]); ``primaryGenreName`` is usually empty.
        # Drop the umbrella "Books" store category.
        genres = [g for g in (r.get("genres") or []) if isinstance(g, str) and g.strip()
                  and g.strip().lower() != "books"]
        return {
            "cover_url": _upscale(r.get("artworkUrl100")),
            "description": (r.get("description") or "").strip() or None,
            "genres": genres,
            "release_date": r.get("releaseDate"),  # ISO 8601, e.g. 2019-11-05T08:00:00Z
            "public_rating": float(rating) if isinstance(rating, (int, float)) else None,
        }
