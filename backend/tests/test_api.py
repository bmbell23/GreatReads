"""Endpoint behavior tests against the running backend.

Read endpoints are asserted for shape; the CRUD endpoints (highlights,
progress, requests) do a create -> read -> update -> delete round-trip and
clean up after themselves so they never leave test rows in the JSON stores.
"""
import unittest

from tests import _base as b


class TestMetaEndpoints(unittest.TestCase):
    def test_version(self):
        self.assertTrue(b.get('/version').json().get('version'))

    def test_build_stamp(self):
        self.assertTrue(b.get('/build-stamp').json().get('stamp'))


class TestBookEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.list = b.get('/books?limit=5').json()

    def test_books_list_shape(self):
        self.assertGreater(self.list['total'], 0)
        bk = self.list['books'][0]
        for k in ('id', 'title', 'author', 'format'):
            self.assertIn(k, bk)

    def test_single_book(self):
        bid = self.list['books'][0]['id']
        j = b.get(f'/books/{bid}').json()
        self.assertEqual(str(j['id']), str(bid))
        self.assertTrue(j.get('title'))

    def test_book_cover(self):
        bid = self.list['books'][0]['id']
        r = b.get(f'/books/{bid}/cover')
        self.assertEqual(r.status_code, 200)
        self.assertIn('image', r.headers.get('Content-Type', ''))

    def test_search(self):
        j = b.get('/search?q=the&limit=3').json()
        self.assertIn('books', j)
        self.assertIn('total', j)


class TestLibraryEndpoint(unittest.TestCase):
    def test_merged_library_shape(self):
        j = b.get('/library?limit=50&offset=0').json()
        self.assertIn('absEnabled', j)
        self.assertGreater(len(j['books']), 0)
        for bk in j['books']:
            self.assertIn(bk.get('mediaTypes'),
                          (['ebook'], ['ebook', 'audiobook'], ['audiobook']))
        if j['absEnabled']:
            dual = [x for x in j['books'] if x.get('mediaTypes') == ['ebook', 'audiobook']]
            for x in dual:
                self.assertTrue(x.get('absId'))

    def test_audiobooks_debug(self):
        j = b.get('/audiobooks').json()
        self.assertIn('absEnabled', j)
        self.assertIn('audiobooks', j)


class TestHighlightsCrud(unittest.TestCase):
    def test_roundtrip(self):
        body = {'type': 'bookmark', 'bookId': '__test__', 'bookTitle': 'T',
                'bookAuthor': 'A', 'anchor': 0, 'page': 1, 'total': 1}
        hid = b.post('/highlights', body).json().get('id')
        self.assertTrue(hid)
        try:
            got = b.get('/highlights?bookId=__test__').json()
            ids = [h['id'] for h in (got if isinstance(got, list) else got.get('items', []))]
            self.assertIn(hid, ids)
            self.assertEqual(b.put(f'/highlights/{hid}', {'note': 'hi'}).status_code, 200)
        finally:
            self.assertIn(b.delete(f'/highlights/{hid}').status_code, (200, 204))


class TestProgressCrud(unittest.TestCase):
    def test_roundtrip(self):
        bid = '__test_progress__'
        self.assertEqual(b.put(f'/progress/{bid}',
                               {'progress': 0.5, 'anchor': 3, 'fontSize': 18}).status_code, 200)
        try:
            j = b.get(f'/progress/{bid}').json()
            self.assertAlmostEqual(j.get('progress'), 0.5)
            self.assertEqual(j.get('anchor'), 3)
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))

    def test_audiobook_fields(self):
        # The audiobook player persists position/duration/mediaType/absId so it
        # can resume to the second and surface in the library "recent" list.
        bid = 'abs:__test_audio__'
        self.assertEqual(b.put(f'/progress/{bid}', {
            'mediaType': 'audiobook', 'absId': '__test_audio__',
            'position': 1234.5, 'duration': 4000, 'progress': 0.30,
            'bookTitle': 'T', 'bookAuthor': 'A',
        }).status_code, 200)
        try:
            j = b.get(f'/progress/{bid}').json()
            self.assertEqual(j.get('mediaType'), 'audiobook')
            self.assertEqual(j.get('absId'), '__test_audio__')
            self.assertAlmostEqual(j.get('position'), 1234.5)
            self.assertAlmostEqual(j.get('duration'), 4000)
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))

    def test_updated_timestamp_on_ebook(self):
        # The library's ebookPctFor() compares rec.updated against
        # localStorage.ts to decide which is fresher.  The field must be a
        # positive epoch-ms integer so that comparison works correctly.
        import time
        bid = '__test_ts_ebook__'
        before_ms = int(time.time() * 1000)
        self.assertEqual(b.put(f'/progress/{bid}',
                               {'progress': 0.25, 'anchor': 1, 'fontSize': 18}).status_code, 200)
        try:
            j = b.get(f'/progress/{bid}').json()
            updated = j.get('updated')
            self.assertIsNotNone(updated, 'updated field must be present on ebook record')
            self.assertIsInstance(updated, (int, float))
            # Must be epoch-ms (>> epoch-s; current year ~1.78e12)
            self.assertGreater(updated, before_ms - 5000,
                               'updated must be a recent epoch-ms timestamp')
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))

    def test_updated_timestamp_on_audiobook(self):
        # Same freshness check applies on the audio side (audioPctFor compares
        # rec.updated against the localStorage snapshot ts).
        import time
        bid = 'abs:__test_ts_audio__'
        before_ms = int(time.time() * 1000)
        self.assertEqual(b.put(f'/progress/{bid}', {
            'mediaType': 'audiobook', 'absId': '__test_ts_audio__',
            'position': 500, 'duration': 2000, 'progress': 0.25,
            'bookTitle': 'T', 'bookAuthor': 'A',
        }).status_code, 200)
        try:
            j = b.get(f'/progress/{bid}').json()
            updated = j.get('updated')
            self.assertIsNotNone(updated, 'updated field must be present on audiobook record')
            self.assertIsInstance(updated, (int, float))
            self.assertGreater(updated, before_ms - 5000,
                               'updated must be a recent epoch-ms timestamp')
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))

    def test_audiobook_absid_in_bulk_list(self):
        # The library's ensureProgressBooksLoaded() populates audioProgressByAbsId
        # keyed by absId from the bulk GET /api/progress response.  If absId is
        # absent from the bulk list, audioPctFor() can never resolve the record
        # and the audio progress bar never renders.
        bid = 'abs:__test_bulk_audio__'
        abs_id = '__test_bulk_audio__'
        self.assertEqual(b.put(f'/progress/{bid}', {
            'mediaType': 'audiobook', 'absId': abs_id,
            'position': 100, 'duration': 1000, 'progress': 0.10,
            'bookTitle': 'T', 'bookAuthor': 'A',
        }).status_code, 200)
        try:
            data = b.get('/progress').json()
            items = data.get('items', [])
            match = next((it for it in items if it.get('bookId') == bid), None)
            self.assertIsNotNone(match, 'audiobook progress must appear in bulk /api/progress list')
            self.assertEqual(match.get('absId'), abs_id,
                             'absId must be preserved in bulk list so audioProgressByAbsId resolves')
            self.assertIsInstance(match.get('updated'), (int, float),
                                  'updated must be present in bulk list for freshness comparison')
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))

    def test_last_writer_wins_advances_updated(self):
        # Two sequential PUTs — the second must increase `updated` so that a
        # stale localStorage snapshot (ts < second updated) will correctly lose
        # the freshness check and the backend value is preferred.
        import time
        bid = '__test_lww__'
        b.put(f'/progress/{bid}', {'progress': 0.1, 'anchor': 0, 'fontSize': 18})
        try:
            first_updated = b.get(f'/progress/{bid}').json().get('updated', 0)
            time.sleep(0.05)   # ensure clock advances even on fast machines
            b.put(f'/progress/{bid}', {'progress': 0.2, 'anchor': 1, 'fontSize': 18})
            j = b.get(f'/progress/{bid}').json()
            self.assertAlmostEqual(j.get('progress'), 0.2)
            self.assertGreaterEqual(j.get('updated', 0), first_updated,
                                    'updated must not go backwards on re-PUT')
        finally:
            self.assertIn(b.delete(f'/progress/{bid}').status_code, (200, 204))


class TestRequestsCrud(unittest.TestCase):
    def test_roundtrip(self):
        rid = b.post('/requests', {'title': '__test__ delete me'}).json().get('id')
        self.assertTrue(rid)
        try:
            self.assertIn(b.post(f'/requests/{rid}/comments',
                                 {'text': 'ping', 'author': 'agent'}).status_code, (200, 201))
            self.assertEqual(b.put(f'/requests/{rid}', {'status': 'Backlog'}).status_code, 200)
        finally:
            self.assertIn(b.delete(f'/requests/{rid}').status_code, (200, 204))


if __name__ == '__main__':
    unittest.main()
