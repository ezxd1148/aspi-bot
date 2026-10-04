import unittest
from unittest.mock import Mock, patch
from test_provider_checks import load_bot


class TallyResetTests(unittest.TestCase):
    def setUp(self):
        self.tally = load_bot().lib.tally_admin

    def test_listing_failure_is_not_reported_as_empty_success(self):
        with patch.object(self.tally.requests, 'get', return_value=Mock(status_code=500)), \
             patch.object(self.tally.requests, 'delete') as delete:
            with self.assertRaises(RuntimeError):
                self.tally.delete_all_submissions('key', 'form')
        delete.assert_not_called()

    def test_deletion_failure_propagates(self):
        listing = Mock(status_code=200, json=Mock(return_value={'submissions': [{'id': 's'}], 'hasMore': False}))
        with patch.object(self.tally.requests, 'get', return_value=listing) as get, \
             patch.object(self.tally.requests, 'delete', return_value=Mock(status_code=403)) as delete:
            with self.assertRaises(RuntimeError):
                self.tally.delete_all_submissions('key', 'form')
            self.assertEqual(get.call_args.kwargs['timeout'], 30)
            self.assertEqual(delete.call_args.kwargs['timeout'], 30)
