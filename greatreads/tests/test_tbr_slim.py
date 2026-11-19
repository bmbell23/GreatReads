"""TBR list slim mode (#411): GET /api/readings/tbr?fields=card."""
from datetime import date

from greatreads.models.book import Book
from greatreads.models.external_import import ExternalImport
from greatreads.models.inventory import Inventory
from greatreads.models.reading import Reading
from greatreads.models.tag import Tag

HEAVY = {"description", "tags", "inventory"}


def _seed(db):
    a = Book(title="Elantris", author_name_first="Brandon", author_name_second="Sanderson",
             description="A long synopsis " * 50, series="Elantris", series_number=1.0,
             date_published=date(2005, 4, 21), word_count=200000, page_count=500)
    b = Book(title="Dune", author_name_first="Frank", author_name_second="Herbert")
    c = Book(title="Done Already")
    a.tags = [Tag(name="Fantasy")]
    db.add_all([a, b, c])
    db.commit()
    db.add_all([
        Inventory(book_id=a.id, owned_ebook=True, owned_audio=True, owned_physical=False),
        Inventory(book_id=b.id, owned_physical=True, shelf_bookshelf="A", shelf_shelf=1),
        # a: one prior finish + a re-read waiting (paused counts as TBR too)
        Reading(book_id=a.id, media="Ebook", date_started=date(2025, 1, 1),
                date_finished_actual=date(2025, 1, 9)),
        Reading(book_id=a.id, media="Ebook", date_started=date(2025, 6, 1),
                date_paused=date(2025, 6, 3)),
        Reading(book_id=b.id, media="Physical"),
        # not TBR: finished / DNF / actively in progress
        Reading(book_id=c.id, media="Audio", date_started=date(2025, 3, 1),
                date_finished_actual=date(2025, 3, 2)),
        Reading(book_id=c.id, media="Audio", date_started=date(2025, 4, 1),
                date_dnf=date(2025, 4, 2)),
        Reading(book_id=c.id, media="Audio", date_started=date(2025, 5, 1)),
        ExternalImport(source="calibre", external_id="42", book_id=a.id, action="linked"),
        ExternalImport(source="audiobookshelf", external_id="abs-uuid", book_id=a.id,
                       action="linked"),
    ])
    db.commit()
    return a.id, b.id


def _norm(rows):
    for r in rows:
        if r.get("book") and "media_owned" in r["book"]:
            r["book"]["media_owned"] = sorted(r["book"]["media_owned"])
    return rows


def test_card_mode_is_default_minus_heavy_fields(client, db):
    _seed(db)
    full = _norm(client.get("/api/readings/tbr").json())
    slim = _norm(client.get("/api/readings/tbr?fields=card").json())
    assert [r["id"] for r in slim] == [r["id"] for r in full]  # same readings, same order
    assert len(slim) == 2
    for f, s in zip(full, slim):
        for k in HEAVY:
            assert k not in s["book"]
        expected = dict(f, book={k: v for k, v in f["book"].items() if k not in HEAVY})
        assert s == expected


def test_card_mode_derived_values(client, db):
    a, b = _seed(db)
    rows = {r["book"]["id"]: r["book"] for r in client.get("/api/readings/tbr?fields=card").json()}
    assert rows[a]["read_count"] == 1
    assert rows[a]["media_owned"] == ["Audio", "Ebook"]
    assert rows[a]["calibre_id"] == "42" and rows[a]["abs_id"] == "abs-uuid"
    assert rows[b]["read_count"] == 0
    assert rows[b]["media_owned"] == ["Physical"]
    assert rows[b]["calibre_id"] is None and rows[b]["abs_id"] is None


def test_default_mode_still_full(client, db):
    a, _ = _seed(db)
    book = next(r["book"] for r in client.get("/api/readings/tbr").json() if r["book"]["id"] == a)
    assert book["description"].startswith("A long synopsis")
    assert book["tags"] == ["Fantasy"]
    assert len(book["inventory"]) == 1


def test_card_mode_empty(client, db):
    assert client.get("/api/readings/tbr?fields=card").json() == []
