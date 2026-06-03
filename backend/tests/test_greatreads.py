"""Integration tests for the GreatReads sync endpoint.

Uses the Flask test client to exercise the matching logic and dry-run safety
without hitting the production GreatReads tracker.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


class TestGreatReadsSync(unittest.TestCase):
    def setUp(self):
        self.app = server.app.test_client()
        self.app.testing = True
        # Mock the data dir so we don't touch real progress.json
        self._orig_load = server._load_progress
        self._orig_lock = server._progress_lock
        server._progress_lock = MagicMock()

    def tearDown(self):
        server._load_progress = self._orig_load
        server._progress_lock = self._orig_lock

    def _mock_progress(self, items):
        server._load_progress = lambda: items

    @patch('requests.get')
    def test_sync_dry_run_matches_by_title_and_format(self, mock_get):
        # 1. Setup local progress state
        self._mock_progress({
            '651': {
                'bookId': '651', 'bookTitle': 'The Second Generation',
                'progress': 0.505, 'updated': 1000
            },
            'abs:77ef': {
                'bookId': 'abs:77ef', 'bookTitle': 'Iron Gold',
                'mediaType': 'audiobook', 'progress': 0.722, 'updated': 2000
            }
        })

        # 2. Mock GreatReads in-progress list
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                'id': 398, 'book': {'title': 'Iron Gold'},
                'media': 'Audio', 'current_percent': 60.9
            },
            {
                'id': 1174, 'book': {'title': 'The Second Generation'},
                'media': 'Ebook', 'current_percent': 58.8
            }
        ]
        mock_get.return_value = mock_resp

        # 3. Request dry-run
        r = self.app.post('/api/greatreads/sync?dry_run=1')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()

        self.assertTrue(data['dryRun'])
        self.assertEqual(data['syncedCount'], 2)
        
        # Verify specific matches
        synced = {s['title']: s for s in data['synced']}
        self.assertEqual(synced['Iron Gold']['percent'], 72.2)
        self.assertEqual(synced['Iron Gold']['media'], 'Audio')
        self.assertEqual(synced['The Second Generation']['percent'], 50.5)
        self.assertEqual(synced['The Second Generation']['media'], 'Ebook')

    @patch('requests.get')
    @patch('requests.put')
    def test_sync_skips_up_to_date_records(self, mock_put, mock_get):
        self._mock_progress({
            '651': {
                'bookId': '651', 'bookTitle': 'The Second Generation',
                'progress': 0.505, 'updated': 1000
            }
        })
        
        # Mock GreatReads already at 50.5%
        mock_get.return_value.json.return_value = [{
            'id': 1174, 'book': {'title': 'The Second Generation'},
            'media': 'Ebook', 'current_percent': 50.5
        }]
        
        r = self.app.post('/api/greatreads/sync')
        data = r.get_json()
        
        self.assertEqual(data['syncedCount'], 0)
        self.assertEqual(data['skipped'][0]['reason'], 'already at 50.5%')
        mock_put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
