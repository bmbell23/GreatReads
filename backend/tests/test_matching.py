"""Pure-logic unit tests for the Calibre<->ABS matching pipeline.

These import server.py directly and exercise the normalization + match_works
functions with synthetic data — no network, no running services required.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


class TestNormalization(unittest.TestCase):
    def test_norm_lowercases_strips_article_and_punct(self):
        self.assertEqual(server._norm('The Great Gatsby!'), 'great gatsby')

    def test_norm_strips_accents(self):
        self.assertEqual(server._norm('Café'), 'cafe')

    def test_norm_handles_none(self):
        self.assertEqual(server._norm(None), '')

    def test_norm_author_collapses_initials(self):
        self.assertEqual(server._norm_author('J.R.R. Tolkien'), 'jrr tolkien')

    def test_norm_author_spaced_initials_equal_unspaced(self):
        # ABS stores "J. R. R. Tolkien"; Calibre stores "J.R.R. Tolkien".
        # Both must normalise to the same key so the title+author match succeeds.
        self.assertEqual(
            server._norm_author('J. R. R. Tolkien'),
            server._norm_author('J.R.R. Tolkien'),
        )
        # Same for "George R. R. Martin" (Calibre) vs "George R.R. Martin" (ABS).
        self.assertEqual(
            server._norm_author('George R. R. Martin'),
            server._norm_author('George R.R. Martin'),
        )


class TestStripEdition(unittest.TestCase):
    CASES = [
        ('Dune (Unabridged)', 'Dune'),
        ('Elantris (Dramatized Adaptation)', 'Elantris'),
        ('Iron Gold (1 of 2) [Dramatized Adaptation]', 'Iron Gold'),
        ('Black Prism (Part 3 of 3)', 'Black Prism'),
        ('02 - Firefight', 'Firefight'),
        ('03. Calamity', 'Calamity'),
        ('Harry Potter (Full-Cast Edition)', 'Harry Potter'),
    ]

    def test_strip_edition_normalizes_to_base_title(self):
        for raw, base in self.CASES:
            self.assertEqual(
                server._norm(server._strip_edition(raw)),
                server._norm(base),
                msg=f'{raw!r} should strip to {base!r}',
            )


class TestFirstAuthor(unittest.TestCase):
    def test_comma_joined_reduces_to_lead(self):
        item = {'author': 'Robert Jordan, Brandon Sanderson'}
        self.assertEqual(server._first_author(item), 'Robert Jordan')

    def test_authors_list_takes_first(self):
        self.assertEqual(server._first_author({'authors': ['Joe Abercrombie']}), 'Joe Abercrombie')

    def test_empty_item(self):
        self.assertEqual(server._first_author({}), '')


class TestNormalizeAbsItem(unittest.TestCase):
    @staticmethod
    def _raw(title):
        return {'id': 'x1', 'mediaType': 'book',
                'media': {'metadata': {'title': title, 'authorName': 'A'}}}

    def test_strips_unabridged_from_display_title(self):
        n = server.normalize_abs_item(self._raw('A Storm of Swords (Unabridged)'))
        self.assertEqual(n['title'], 'A Storm of Swords')
        self.assertEqual(n['_rawTitle'], 'A Storm of Swords (Unabridged)')

    def test_clean_title_never_empty(self):
        # Pathological all-marker title falls back to the raw title.
        n = server.normalize_abs_item(self._raw('(Unabridged)'))
        self.assertEqual(n['title'], '(Unabridged)')


def _abs(absId='a1', title='T', isbn='', asin='', author='', authors=None):
    return {'absId': absId, 'title': title, 'isbn': isbn, 'asin': asin,
            'author': author, 'authors': authors, 'audioCover': None,
            'narrators': [], 'audiobook': None, 'mediaTypes': ['audiobook']}


def _cal(cid=1, title='T', isbn='', asin='', author='', authors=None):
    return {'id': cid, 'title': title, 'isbn': isbn, 'asin': asin,
            'author': author, 'authors': authors}


class TestMatchWorks(unittest.TestCase):
    def setUp(self):
        # Neutralize on-disk manual links so tests are deterministic.
        self._orig_links = server._load_links
        server._load_links = lambda: {}

    def tearDown(self):
        server._load_links = self._orig_links

    def _one(self, cal, abslist, **kw):
        return server.match_works([cal], abslist, **kw)[0]

    def test_title_author_match(self):
        m = self._one(_cal(title='Dune', author='Frank Herbert'),
                      [_abs(title='Dune', author='Frank Herbert')])
        self.assertEqual(m['mediaTypes'], ['ebook', 'audiobook'])
        self.assertEqual(m['absId'], 'a1')

    def test_edition_strip_tier_matches(self):
        m = self._one(_cal(title='Dune', author='Frank Herbert'),
                      [_abs(title='Dune (Unabridged)', author='Frank Herbert')])
        self.assertEqual(m['mediaTypes'], ['ebook', 'audiobook'])

    def test_isbn_match_beats_title(self):
        m = self._one(_cal(title='Wholly Different', isbn='123', author='X'),
                      [_abs(title='Audio Name', isbn='123', author='Y')])
        self.assertEqual(m['mediaTypes'], ['ebook', 'audiobook'])

    def test_asin_match(self):
        m = self._one(_cal(title='A', asin='B007', author='X'),
                      [_abs(title='Z', asin='B007', author='Y')])
        self.assertEqual(m['mediaTypes'], ['ebook', 'audiobook'])

    def test_author_gate_prevents_false_positive(self):
        m = self._one(_cal(title='Dune', author='Alice'),
                      [_abs(title='Dune', author='Bob')])
        self.assertEqual(m['mediaTypes'], ['ebook'])
        self.assertNotIn('absId', m)

    def test_audio_only_appended_when_included(self):
        merged = server.match_works([_cal(title='X', author='A')],
                                    [_abs(absId='solo', title='Solo', author='B')])
        self.assertTrue(any(x.get('absId') == 'solo'
                            and x.get('mediaTypes') == ['audiobook'] for x in merged))

    def test_audio_only_suppressed_on_paginated_pages(self):
        merged = server.match_works([_cal(title='X', author='A')],
                                    [_abs(absId='solo', title='Solo', author='B')],
                                    include_audio_only=False)
        self.assertFalse(any(x.get('absId') == 'solo' for x in merged))


if __name__ == '__main__':
    unittest.main()
