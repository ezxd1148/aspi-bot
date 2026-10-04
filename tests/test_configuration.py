import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib import config


class ConfigurationTests(unittest.TestCase):
    def test_explicit_group_wins_over_previous_admin(self):
        with patch.dict(os.environ, {'ADMIN_GROUP_ID': '-100123', 'ADMIN_CHAT_ID': '456'}, clear=True):
            self.assertEqual(config.review_destination(), ('-100123', 'ADMIN_GROUP_ID'))

    def test_invalid_explicit_group_never_falls_back(self):
        for value in ('', '123', '0', 'group-name', '@reviewers', '-0'):
            with self.subTest(value=value), patch.dict(os.environ, {
                    'ADMIN_GROUP_ID': value, 'ADMIN_CHAT_ID': '456'}, clear=True):
                with self.assertRaises(ValueError):
                    config.review_destination()

    def test_legacy_group_and_private_ids_remain_supported(self):
        for value in ('123', '-100123'):
            with patch.dict(os.environ, {'ADMIN_CHAT_ID': value}, clear=True):
                self.assertEqual(config.review_destination(), (value, 'ADMIN_CHAT_ID'))

    def test_missing_destination_is_error(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            config.review_destination()

    def test_checkout_env_overrides_stale_inherited_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / '.env'
            env.write_text('ADMIN_CHAT_ID=-100987\nTELEGRAM_BOT_TOKEN=new-token\n')
            with patch.object(config, 'ENV_FILE', env), patch.dict(os.environ, {
                    'ADMIN_CHAT_ID': '123', 'TELEGRAM_BOT_TOKEN': 'old-token'}, clear=True):
                config.load_environment()
                self.assertEqual(config.review_destination(), ('-100987', 'ADMIN_CHAT_ID'))
                self.assertEqual(os.environ['TELEGRAM_BOT_TOKEN'], 'new-token')

    def test_duplicate_review_keys_are_rejected_without_secret_values(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / '.env'
            env.write_text('ADMIN_GROUP_ID=-100123\nexport ADMIN_GROUP_ID=-100987\n')
            with patch.object(config, 'ENV_FILE', env), self.assertRaises(ValueError) as error:
                config.load_environment()
            self.assertIn('Duplicate ADMIN_GROUP_ID', str(error.exception))
            self.assertNotIn('-100987', str(error.exception))
