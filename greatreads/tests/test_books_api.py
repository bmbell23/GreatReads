from greatreads.models.book import Book


def _add(db, **kw):
    b = Book(**kw)
    db.add(b)
    db.commit()
    return b.id


def test_list_books_empty(client):
    r = client.get("/api/books/")
    assert r.status_code == 200
    assert r.json() == []


def test_list_books_returns_rows(client, db):
    _add(db, title="Mistborn", author_name_first="Brandon", author_name_second="Sanderson")
    r = client.get("/api/books/")
    assert r.status_code == 200
    data = r.json()
    assert [b["title"] for b in data] == ["Mistborn"]
    assert data[0]["is_read"] is False


def test_list_books_search_and_author_filters(client, db):
    _add(db, title="Mistborn", author_name_first="Brandon", author_name_second="Sanderson")
    _add(db, title="Dune", author_name_first="Frank", author_name_second="Herbert")
    assert [b["title"] for b in client.get("/api/books/?search=dun").json()] == ["Dune"]
    assert [b["title"] for b in client.get("/api/books/?author=sanderson").json()] == ["Mistborn"]
    assert client.get("/api/books/?search=zzz").json() == []


def test_list_books_pagination(client, db):
    for i in range(5):
        _add(db, title=f"Book {i}")
    assert len(client.get("/api/books/?limit=2").json()) == 2
    assert len(client.get("/api/books/?skip=4&limit=10").json()) == 1


def test_list_books_rejects_bad_limit(client):
    assert client.get("/api/books/?limit=0").status_code == 422


def test_get_book(client, db):
    bid = _add(db, title="Elantris", series="Elantris", series_number=1.0)
    r = client.get(f"/api/books/{bid}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Elantris"
    assert body["inventory"] == []
    assert body["content_auto"] is None


def test_get_book_404(client):
    r = client.get("/api/books/9999")
    assert r.status_code == 404
    assert r.json()["detail"] == "Book not found"


def test_authors_by_title_falls_back_to_passed_author(client):
    r = client.get("/api/books/authors-by-title", params={"title": "Nope", "author": "Some One"})
    assert r.status_code == 200
    assert r.json() == {"authors": ["Some One"], "display": "Some One"}


def test_authors_by_title_uses_book_author(client, db):
    _add(db, title="Elantris", author_name_first="Brandon", author_name_second="Sanderson")
    r = client.get("/api/books/authors-by-title", params={"title": "Elantris"})
    assert r.status_code == 200
    assert r.json()["authors"] == ["Brandon Sanderson"]


def test_join_authors():
    from greatreads.routes.books import _join_authors
    assert _join_authors([]) == ""
    assert _join_authors(["A"]) == "A"
    assert _join_authors(["A", "B"]) == "A & B"
    assert _join_authors(["A", "B", "C"]) == "A, B & C"
