"""Library list slim mode (#405): GET /api/library/books?fields=card."""
from datetime import date

from greatreads.models.book import Book
from greatreads.models.external_import import ExternalImport
from greatreads.models.inventory import Inventory
from greatreads.models.reading import Reading

CARD_KEYS = {
    "id", "title", "author", "author_sorted", "universe", "series", "series_number",
    "date_published", "word_count", "page_count", "cover", "cover_version",
    "read_count", "is_read", "is_in_progress", "media_owned", "calibre_id", "abs_id",
}


def _seed(db):
    a = Book(title="Elantris", author_name_first="Brandon", author_name_second="Sanderson",
             description="A long synopsis " * 50, series="Elantris", series_number=1.0,
             date_published=date(2005, 4, 21), word_count=200000, page_count=500)
    b = Book(title="Dune", author_name_first="Frank", author_name_second="Herbert")
    c = Book(title="Not Owned")
    db.add_all([a, b, c])
    db.commit()
    db.add_all([
        Inventory(book_id=a.id, owned_ebook=True, owned_audio=True, owned_physical=False),
        Inventory(book_id=b.id, owned_physical=True, shelf_bookshelf="A", shelf_shelf=1),
        Inventory(book_id=c.id, owned_ebook=False, owned_audio=False, owned_physical=False),
        Reading(book_id=a.id, media="Ebook", date_started=date(2025, 1, 1),
                date_finished_actual=date(2025, 1, 9)),
        Reading(book_id=a.id, media="Ebook", date_started=date(2025, 6, 1)),
        Reading(book_id=b.id, media="Physical", date_started=date(2025, 2, 1),
                date_dnf=date(2025, 2, 5)),
        ExternalImport(source="calibre", external_id="42", book_id=a.id, action="linked"),
        ExternalImport(source="audiobookshelf", external_id="abs-uuid", book_id=a.id,
                       action="linked"),
    ])
    db.commit()
    return a.id, b.id


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_card_mode_shape_has_no_heavy_fields(client, db):
    a, b = _seed(db)
    r = client.get("/api/library/books?limit=10000&fields=card")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2  # the unowned book is excluded, as in default mode
    for row in rows:
        assert set(row) == CARD_KEYS
        assert "description" not in row and "inventory" not in row and "readings" not in row


def test_card_mode_derived_values(client, db):
    a, b = _seed(db)
    rows = _by_id(client.get("/api/library/books?limit=10000&fields=card").json())
    ra, rb = rows[a], rows[b]
    assert ra["read_count"] == 1 and ra["is_read"] is True
    assert ra["is_in_progress"] is True            # the reread
    assert ra["media_owned"] == ["Audio", "Ebook"]  # sorted, deterministic
    assert ra["calibre_id"] == "42" and ra["abs_id"] == "abs-uuid"
    assert ra["author"] == "Brandon Sanderson" and ra["author_sorted"] == "Sanderson, Brandon"
    assert ra["date_published"] == "2005-04-21"
    assert rb["read_count"] == 0 and rb["is_read"] is False
    assert rb["is_in_progress"] is False            # DNF is not in-progress (#271)
    assert rb["media_owned"] == ["Physical"]
    assert rb["calibre_id"] is None and rb["abs_id"] is None


def test_card_mode_agrees_with_default_mode(client, db):
    _seed(db)
    full = _by_id(client.get("/api/library/books?limit=10000").json())
    slim = _by_id(client.get("/api/library/books?limit=10000&fields=card").json())
    assert full.keys() == slim.keys()
    for bid, s in slim.items():
        f = full[bid]
        for k in CARD_KEYS - {"media_owned"}:
            assert s[k] == f[k], (bid, k)
        assert s["media_owned"] == sorted(f["media_owned"])


def test_card_mode_respects_filters_and_sort(client, db):
    _seed(db)
    rows = client.get("/api/library/books?fields=card&sort_by=title").json()
    assert [r["title"] for r in rows] == ["Dune", "Elantris"]
    rows = client.get("/api/library/books?fields=card&media_owned=audio").json()
    assert [r["title"] for r in rows] == ["Elantris"]


def test_card_mode_is_deterministic(client, db):
    _seed(db)
    one = client.get("/api/library/books?limit=10000&fields=card").text
    two = client.get("/api/library/books?limit=10000&fields=card").text
    assert one == two


def test_default_mode_unchanged(client, db):
    a, _ = _seed(db)
    rows = _by_id(client.get("/api/library/books?limit=10000").json())
    ra = rows[a]
    assert ra["description"].startswith("A long synopsis")
    assert len(ra["readings"]) == 2
    assert len(ra["inventory"]) == 1 and ra["inventory"][0]["owned_ebook"] is True
    assert ra["read_count"] == 1 and ra["is_read"] is True
    assert "tags" in ra


def test_unknown_fields_value_falls_back_to_default(client, db):
    a, _ = _seed(db)
    rows = _by_id(client.get("/api/library/books?fields=bogus").json())
    assert "inventory" in rows[a] and "description" in rows[a]
