"""OpenLibrary client (#69) — free, no API key.

Used to enrich the News feed and the Add-Book flow with data Google Books lacks:
- **first_publish_year** (search.json) → reprint detection that survives pseudonyms
  (Asimov's 1950s Lucky Starr books fail an author-scoped Google lookup; OpenLibrary
  keys on the work's original year).
- **physical_format** (binding: Hardcover/Paperback) and page count by ISBN.
- **covers** by cover-id, to fill Google's gaps.

Polite usage: a descriptive User-Agent and a small inter-request delay.
"""

import re
from time import sleep
from typing import Optional

import requests

_UA = "GreatReads/1.0 (personal reading tracker; contact via app)"
_SEARCH = "https://openlibrary.org/search.json"
_ISBN = "https://openlibrary.org/isbn/{}.json"
_COVER_ID = "https://covers.openlibrary.org/b/id/{}-L.jpg"


def _norm_binding(fmt: Optional[str]) -> Optional[str]:
    if not fmt:
        return None
    f = fmt.lower()
    if "hard" in f:                       # Hardcover, Hardback
        return "Hardcover"
    if "mass market" in f:
        return "Paperback"
    if "paper" in f or "trade" in f:      # Paperback, Trade Paperback
        return "Paperback"
    return None


class OpenLibraryClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    def first_publish_year(self, title: str, author: str) -> Optional[int]:
        """The work's earliest publication year (across editions/pseudonyms), or None."""
        try:
            r = self.session.get(_SEARCH, params={
                "title": title, "author": author,
                "fields": "first_publish_year", "limit": 1}, timeout=10)
            r.raise_for_status()
            docs = r.json().get("docs", [])
            sleep(0.2)
            if docs and isinstance(docs[0].get("first_publish_year"), int):
                return docs[0]["first_publish_year"]
        except (requests.exceptions.RequestException, ValueError):
            return None
        return None

    def edition_by_isbn(self, isbn: str) -> dict:
        """{binding, pages, cover_url} for an ISBN. Empty dict on miss."""
        try:
            r = self.session.get(_ISBN.format(isbn), timeout=10)
            sleep(0.2)
            if r.status_code != 200:
                return {}
            d = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return {}
        covers = d.get("covers") or []
        cover_id = next((c for c in covers if isinstance(c, int) and c > 0), None)
        return {
            "binding": _norm_binding(d.get("physical_format")),
            "pages": d.get("number_of_pages") if isinstance(d.get("number_of_pages"), int) else None,
            "cover_url": _COVER_ID.format(cover_id) if cover_id else None,
        }
