from datetime import date

from greatreads.models.book import Book
from greatreads.models.reading import Reading


def _book(db, title="Elantris"):
    b = Book(title=title)
    db.add(b)
    db.commit()
    return b.id


def _post(client, book_id, **extra):
    r = client.post("/api/readings/", json={"book_id": book_id, "media": "Ebook", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_first_reading_is_not_reread(client, db):
    assert _post(client, _book(db))["reread"] is False


def test_reread_inferred_when_finished_reading_exists(client, db):
    bid = _book(db)
    db.add(Reading(book_id=bid, media="Ebook", date_finished_actual=date(2025, 1, 1)))
    db.commit()
    assert _post(client, bid)["reread"] is True


def test_unfinished_reading_does_not_imply_reread(client, db):
    bid = _book(db)
    db.add(Reading(book_id=bid, media="Ebook"))
    db.commit()
    assert _post(client, bid)["reread"] is False


def test_finished_other_book_does_not_imply_reread(client, db):
    other = _book(db, "Dune")
    bid = _book(db)
    db.add(Reading(book_id=other, media="Ebook", date_finished_actual=date(2025, 1, 1)))
    db.commit()
    assert _post(client, bid)["reread"] is False


def test_explicit_reread_true_is_kept(client, db):
    assert _post(client, _book(db), reread=True)["reread"] is True
