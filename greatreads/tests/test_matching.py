"""Pure-function tests for the ebook/audiobook linking + normalization code."""
import pytest

from greatreads.services.import_service import (
    _best_match, _split_author, _split_authors, _title_core_tokens,
    _word_tokens, is_blank_author, title_similarity,
)
from greatreads import ereader_api as ea


def _existing(book_id, title, author, series=None, num=None, year=None, universe=None):
    return (book_id, title, author, _word_tokens(title), series, num, year,
            _word_tokens(author), universe)


# ---- import_service normalization ------------------------------------------

def test_word_tokens_ampersand_equals_and():  # #148
    assert _word_tokens("Mother of Death & Dawn") == _word_tokens("Mother of Death and Dawn")


def test_word_tokens_strips_punctuation_and_case():
    assert _word_tokens("Dark Matter: A Novel!") == frozenset({"dark", "matter", "a", "novel"})


def test_title_core_tokens():
    assert _title_core_tokens("Dark Matter: A Novel") == frozenset({"dark", "matter"})
    assert _title_core_tokens("Dark Matter") is None
    assert _title_core_tokens("Dark Matter:") is None


def test_split_author():
    assert _split_author("Brandon Sanderson") == ("Brandon", "Sanderson")
    assert _split_author("J. R. R. Tolkien") == ("J. R. R.", "Tolkien")
    assert _split_author("Plato") == ("Plato", None)
    assert _split_author("  ") == (None, None)


def test_split_authors_co_authors():  # #324
    assert _split_authors("Brandon Sanderson, Janci Patterson") == ["Brandon Sanderson", "Janci Patterson"]
    assert _split_authors("A Author & B Writer") == ["A Author", "B Writer"]


def test_split_authors_last_first_stays_whole():
    assert _split_authors("Sanderson, Brandon") == ["Sanderson, Brandon"]
    assert _split_authors("") == []


@pytest.mark.parametrize("val,expected", [
    (None, True), ("", True), ("  ", True), ("Unknown", True), ("  unKNOWN ", True),
    ("Brandon Sanderson", False),
])
def test_is_blank_author(val, expected):
    assert is_blank_author(val) is expected


def test_title_similarity_subtitle_tolerance():  # #204
    full = _word_tokens("Dark Matter: A Novel")
    other = _word_tokens("Dark Matter")
    sim = title_similarity(full, _title_core_tokens("Dark Matter: A Novel"), "Dark Matter", other)
    assert sim == 1.0


def test_title_similarity_different_subtitles_do_not_collapse():
    a, b = "Foo: Part One", "Foo: Part Two"
    sim = title_similarity(_word_tokens(a), _title_core_tokens(a), b, _word_tokens(b))
    assert sim < 1.0


# ---- _best_match ------------------------------------------------------------

def test_best_match_exact_is_certain():
    ex = [_existing(1, "Elantris", "Brandon Sanderson", "Elantris", 1.0, "2005")]
    m = _best_match("Elantris", "Brandon Sanderson", "Elantris", 1.0, "2005", ex)
    assert m["id"] == 1 and m["score"] == 1.0


def test_best_match_ampersand_title_links():  # #148
    ex = [_existing(7, "Mother of Death and Dawn", "Some Author")]
    m = _best_match("Mother of Death & Dawn", "Some Author", None, None, None, ex)
    assert m is not None and m["id"] == 7 and m["score"] >= 0.9


def test_best_match_co_author_subset_links():  # #324
    ex = [_existing(3, "Skyward", "Brandon Sanderson")]
    m = _best_match("Skyward", "Brandon Sanderson, Janci Patterson", None, None, None, ex)
    assert m is not None and m["id"] == 3


def test_best_match_co_authors_in_other_order_links():  # #383
    ex = [_existing(4, "Blightfall", "Brandon Sanderson, Janci Patterson")]
    m = _best_match("Blightfall", "Janci Patterson, Brandon Sanderson", None, None, None, ex)
    assert m is not None and m["id"] == 4 and m["score"] >= 0.9


def test_best_match_unrelated_returns_none():
    ex = [_existing(1, "Elantris", "Brandon Sanderson")]
    assert _best_match("Dune", "Frank Herbert", None, None, None, ex) is None


def test_best_match_blank_title_returns_none():
    assert _best_match("", "X", None, None, None, [_existing(1, "A", "B")]) is None


def test_best_match_unknown_author_is_neutral():  # #357
    ex = [_existing(5, "Elantris", "Brandon Sanderson")]
    m = _best_match("Elantris", "Unknown", None, None, None, ex)
    assert m is not None and m["id"] == 5


def test_best_match_prefers_owned_on_tie():  # #344
    ex = [_existing(1, "Elantris", "Brandon Sanderson"),
          _existing(2, "Elantris", "Brandon Sanderson")]
    assert _best_match("Elantris", "Brandon Sanderson", None, None, None, ex)["id"] == 1
    assert _best_match("Elantris", "Brandon Sanderson", None, None, None, ex,
                       prefer_ids={2})["id"] == 2


# ---- ereader_api matching helpers ------------------------------------------

def test_norm_drops_article_accents_punctuation():
    assert ea._norm("The Name of the Wind!") == "name of the wind"
    assert ea._norm("Café  Society") == "cafe society"


def test_norm_author_initials():
    assert ea._norm_author("J. R. R. Tolkien") == ea._norm_author("J.R.R. Tolkien") == "jrr tolkien"


def test_all_authors_comma_split_and_dedupe():  # #383
    assert ea._all_authors({"author": "Janci Patterson, Brandon Sanderson"}) == \
        ["janci patterson", "brandon sanderson"]
    assert ea._all_authors({"authors": ["Brandon Sanderson", "Janci Patterson"]}) == \
        ["brandon sanderson", "janci patterson"]


def test_first_author():
    assert ea._first_author({"author": "Robert Jordan, Brandon Sanderson"}) == "Robert Jordan"
    assert ea._first_author({"authors": ["A B"]}) == "A B"


def test_strip_edition_and_kind():
    assert ea._strip_edition("Title (Unabridged) [x]").split() == ["Title"]
    assert ea._strip_edition("03 - Title") == "Title"
    assert ea._kind_of({"title": "X (Dramatized Adaptation)"}) == "dramatized"
    assert ea._kind_of({"title": "X", "publisher": "GraphicAudio"}) == "dramatized"
    assert ea._kind_of({"title": "X"}) == "audiobook"


def test_split_abs_series():
    assert ea._split_abs_series("Dresden Files #10.4") == ("Dresden Files", 10.4)
    assert ea._split_abs_series("Standalone") == ("Standalone", None)


def test_parse_part():
    assert ea._parse_part("Iron Gold (Part 1 of 2)") == (1, 2)
    assert ea._parse_part("Iron Gold") == (None, None)


def test_normalize_links_shapes():
    out = ea._normalize_links({"1": "abs1", "2": ["a", "b"],
                               "3": {"editions": [{"kind": "dramatized", "parts": ["z"]}]},
                               "4": "  ", "5": 12})
    assert out["1"] == [{"parts": ["abs1"]}]
    assert out["2"] == [{"parts": ["a", "b"]}]
    assert out["3"][0]["kind"] == "dramatized"
    assert "4" not in out and "5" not in out


# ---- match_works (the #383 end-to-end link) ---------------------------------

def _abs(abs_id, title, author, **kw):
    d = {"absId": abs_id, "title": title, "author": author,
         "audiobook": {"duration": 3600}, "narrators": ["N"]}
    d.update(kw)
    return d


@pytest.fixture()
def no_links(monkeypatch):
    monkeypatch.setattr(ea, "_load_links", lambda: {})


def test_match_works_links_co_authors_in_different_order(no_links):  # #383
    cal = [{"id": 10, "title": "Blightfall", "authors": ["Brandon Sanderson", "Janci Patterson"]}]
    abs_items = [_abs("a1", "Blightfall", "Janci Patterson, Brandon Sanderson")]
    merged = ea.match_works(cal, abs_items)
    assert len(merged) == 1
    assert merged[0]["mediaTypes"] == ["ebook", "audiobook"]
    assert merged[0]["absId"] == "a1"


def test_match_works_unmatched_audio_only_and_flag(no_links):
    cal = [{"id": 1, "title": "Elantris", "authors": ["Brandon Sanderson"]}]
    abs_items = [_abs("a9", "Totally Different", "Someone Else")]
    merged = ea.match_works(cal, abs_items)
    assert [m["mediaTypes"] for m in merged] == [["ebook"], ["audiobook"]]
    assert len(ea.match_works(cal, abs_items, include_audio_only=False)) == 1


def test_match_works_different_author_same_title_not_linked(no_links):
    cal = [{"id": 1, "title": "Elantris", "authors": ["Brandon Sanderson"]}]
    merged = ea.match_works(cal, [_abs("a2", "Elantris", "Other Person")], include_audio_only=False)
    assert merged[0]["mediaTypes"] == ["ebook"]


def test_match_works_isbn_link(no_links):
    cal = [{"id": 1, "title": "Whatever", "authors": ["X Y"], "isbn": "123"}]
    merged = ea.match_works(cal, [_abs("a3", "Other", "Z Q", isbn="123")])
    assert merged[0]["mediaTypes"] == ["ebook", "audiobook"]
