import ast
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib import moderation


def response(answer=None, status=200, data=None):
    return Mock(status_code=status, json=Mock(return_value=(
        data if data is not None else {'choices': [{'message': {'content': answer}}]}
    )))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.api = moderation.APIS[0]
        self.env = patch.dict(os.environ, {self.api['key_env']: 'test-key'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    @patch.object(moderation.requests, 'post')
    def test_both_samples_use_real_prompt_and_provider_settings(self, post):
        post.side_effect = [response('CLEAN'), response('FLAGGED')]
        result = moderation.test_provider(self.api)
        self.assertIn('CLEAN: PASS; FLAGGED: PASS', result)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.args[0], self.api['url'])
            payload = call.kwargs['json']
            self.assertEqual(payload['model'], self.api['model'])
            self.assertEqual(payload['messages'][0]['content'], moderation.SYSTEM_PROMPT)
            self.assertEqual(call.kwargs['timeout'], self.api['timeout'])
        self.assertIn('g#y', post.call_args.kwargs['json']['messages'][1]['content'])

    @patch.object(moderation.requests, 'post')
    def test_missing_key_never_calls_api(self, post):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('SKIPPED', moderation.test_provider(self.api))
        post.assert_not_called()

    @patch.object(moderation.requests, 'post')
    def test_errors_do_not_fall_back_or_expose_response(self, post):
        for status in (401, 402, 403, 429, 500):
            with self.subTest(status=status):
                post.reset_mock()
                post.return_value = response(status=status, data={'error': 'secret'})
                result = moderation.test_provider(self.api)
                self.assertIn(f'HTTP {status}', result)
                self.assertNotIn('secret', result)
                self.assertEqual(post.call_count, 1)
        post.side_effect = moderation.requests.Timeout('secret')
        self.assertIn('timed out', moderation.test_provider(self.api))
        post.side_effect = moderation.requests.ConnectionError('secret')
        self.assertIn('connection/request error', moderation.test_provider(self.api))

    @patch.object(moderation.requests, 'post')
    def test_wrong_empty_and_malformed_answers_fail(self, post):
        for first in (response('FLAGGED'), response('CLEAN because...'),
                      response(''), response(data={'choices': []})):
            with self.subTest(first=first):
                post.side_effect = [first, response('FLAGGED')]
                self.assertIn('CLEAN: FAIL', moderation.test_provider(self.api))

    @patch.object(moderation.requests, 'post')
    def test_existing_moderation_still_rotates_on_http_failure(self, post):
        with patch.dict(os.environ, {a['key_env']: 'test-key' for a in moderation.APIS}):
            post.side_effect = [response(status=429), response('CLEAN')]
            self.assertEqual(moderation.moderate_text('hello'), 'clean')
            self.assertEqual(post.call_args.args[0], moderation.APIS[1]['url'])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Exercise the actual handler without importing bot startup/environment setup.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'src/bot.py').read_text())
        handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == 'testbots_command')
        self.provider = Mock(return_value='CLEAN: PASS; FLAGGED: PASS')
        namespace = {'asyncio': asyncio, 'ADMIN_CHAT_ID': '123',
                     'lib': SimpleNamespace(moderation=SimpleNamespace(
                         APIS=moderation.APIS, test_provider=self.provider))}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<handler>', 'exec'), namespace)
        self.handler = namespace['testbots_command']
        self.reply = AsyncMock()
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
                                      message=SimpleNamespace(reply_text=self.reply))
        self.context = SimpleNamespace(bot_data={})

    async def test_non_admin_cannot_trigger_requests(self):
        self.update.effective_chat.id = 456
        await self.handler(self.update, self.context)
        self.provider.assert_not_called()
        self.assertIn('Admin only', self.reply.call_args.args[0])

    async def test_all_providers_report_and_lock_is_released(self):
        await self.handler(self.update, self.context)
        self.assertEqual(self.provider.call_count, len(moderation.APIS))
        for api in moderation.APIS:
            self.assertIn(api['name'], self.reply.call_args.args[0])
        self.assertNotIn('provider_test_running', self.context.bot_data)

    async def test_duplicate_run_is_rejected(self):
        self.context.bot_data['provider_test_running'] = True
        await self.handler(self.update, self.context)
        self.provider.assert_not_called()

    async def test_lock_released_on_telegram_failure(self):
        self.reply.side_effect = RuntimeError('Telegram unavailable')
        with self.assertRaises(RuntimeError):
            await self.handler(self.update, self.context)
        self.assertNotIn('provider_test_running', self.context.bot_data)


if __name__ == '__main__':
    unittest.main()
