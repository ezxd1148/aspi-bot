import importlib
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib import moderation


def load_bot():
    import lib.config
    with patch.dict(os.environ, {
        'TALLY_API_KEY': 'test-key', 'FORM_ID': 'test-form',
        'TELEGRAM_BOT_TOKEN': '123:test-token', 'TELEGRAM_CHANNEL_ID': '-10099',
        'ADMIN_CHAT_ID': '123', 'DATA_DIR': '/tmp/aspi-bot-test-state',
    }, clear=True), patch.object(lib.config, 'load_environment'):
        return importlib.import_module('bot')


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
            self.assertNotIn('reasoning_effort', payload)
            self.assertEqual(payload['reasoning'], {'exclude': True})
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
    def test_response_failures_are_specific_and_do_not_expose_raw_text(self, post):
        cases = [
            ({'error': {'code': 429, 'message': 'secret'}}, 'API error in response body (code 429)'),
            ({'choices': []}, 'empty choices array'),
            ({}, 'missing or invalid choices array'),
            ({'choices': [{'message': None}]}, 'missing or invalid message object'),
            ({'choices': [{'message': {'content': ['secret']}}]}, 'content is not a text string'),
            ({'choices': [{'finish_reason': 'length', 'message': {'content': 'CLEAN'}}]}, 'token limit reached'),
            ({'choices': [{'finish_reason': 'content_filter'}]}, 'provider content filter blocked'),
            ({'choices': [{'message': {'refusal': 'secret'}}]}, 'provider refused the classification'),
            ({'choices': [{'message': {'content': None, 'reasoning_content': 'CLEAN'}}]}, 'empty final answer'),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                post.side_effect = [response(data=data), response('FLAGGED')]
                result = moderation.test_provider(self.api)
                self.assertIn(f'CLEAN: FAIL - {expected}', result)
                self.assertNotIn('secret', result)
                self.assertIn('FLAGGED: PASS', result)

    @patch.object(moderation.requests, 'post')
    def test_non_json_response_is_not_reported_as_connection_error(self, post):
        bad = response()
        bad.json.side_effect = ValueError('secret')
        post.side_effect = [bad, response('FLAGGED')]
        result = moderation.test_provider(self.api)
        self.assertIn('response is not valid JSON', result)
        self.assertNotIn('secret', result)

    @patch.object(moderation.requests, 'post')
    def test_existing_moderation_still_rotates_on_http_failure(self, post):
        with patch.dict(os.environ, {a['key_env']: 'test-key' for a in moderation.APIS}):
            post.side_effect = [response(status=429), response('CLEAN')]
            self.assertEqual(moderation.moderate_text('hello'), 'clean')
            self.assertEqual(post.call_args.args[0], moderation.APIS[1]['url'])


    @patch.object(moderation.requests, 'post')
    def test_borderline_flag_gets_note_from_same_provider(self, post):
        post.side_effect = [response('FLAGGED'), response(
            '{"clear_violation": false, "reason": "Neutral race mention triggers the channel rule."}')]
        result = moderation.moderate_submission('test')
        self.assertEqual(result['result'], 'flagged')
        self.assertIn('Neutral race mention', result['reason'])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], post.call_args_list[1].args[0])
        self.assertIn('ADMIN REVIEW-NOTE MODE',
                      post.call_args.kwargs['json']['messages'][0]['content'])

    @patch.object(moderation.requests, 'post')
    def test_clear_flag_or_note_failure_never_changes_classification(self, post):
        for note in [response('{"clear_violation": true, "reason": "ignored"}'),
                     response('not JSON'), response(status=429),
                     response('{"clear_violation": "false", "reason": "ignored"}'),
                     response('{"clear_violation": false, "reason": 123}')]:
            post.side_effect = [response('FLAGGED'), note]
            self.assertEqual(moderation.moderate_submission('test'),
                             {'result': 'flagged', 'reason': ''})
        post.side_effect = [response('FLAGGED'), moderation.requests.Timeout('secret')]
        self.assertEqual(moderation.moderate_submission('test'), {'result': 'flagged', 'reason': ''})

    @patch.object(moderation.requests, 'post')
    def test_clean_needs_no_note_request_and_note_length_is_bounded(self, post):
        post.return_value = response('CLEAN')
        self.assertEqual(moderation.moderate_submission('hello'), {'result': 'clean', 'reason': ''})
        self.assertEqual(post.call_count, 1)
        post.side_effect = [response('FLAGGED'), response(
            '{"clear_violation": false, "reason": "' + 'x' * 500 + '"}')]
        self.assertEqual(len(moderation.moderate_submission('test')['reason']), 240)

    @patch.object(moderation.requests, 'post')
    def test_error_bodies_and_embedded_classification_are_never_approved(self, post):
        post.return_value = response('Not CLEAN, this is FLAGGED')
        self.assertEqual(moderation.moderate_text('test'), 'error')
        post.return_value = response(data={'error': {'code': 400}})
        self.assertEqual(moderation.moderate_text('test'), 'error')

    @patch.object(moderation.requests, 'post')
    def test_groq_probe_uses_supported_reasoning_settings(self, post):
        api = next(a for a in moderation.APIS if a['name'] == 'groq')
        post.side_effect = [response('CLEAN'), response('FLAGGED')]
        with patch.dict(os.environ, {'GROQ_API_KEY': 'test-key'}, clear=True):
            self.assertIn('CLEAN: PASS; FLAGGED: PASS', moderation.test_provider(api))
        for call in post.call_args_list:
            self.assertEqual(call.args[0], 'https://api.groq.com/openai/v1/chat/completions')
            payload = call.kwargs['json']
            self.assertEqual(payload['model'], api['model'])
            self.assertEqual(payload['reasoning_effort'], 'low')
            self.assertFalse(payload['include_reasoning'])
            self.assertNotIn('reasoning_format', payload)

    @patch.object(moderation.requests, 'post')
    def test_groq_rate_limit_falls_back_to_nvidia(self, post):
        with patch.dict(os.environ, {a['key_env']: 'test-key' for a in moderation.APIS}, clear=True):
            post.side_effect = [response(status=429), response(status=429), response('FLAGGED')]
            self.assertEqual(moderation.moderate_text('test'), 'flagged')
        self.assertEqual([call.args[0] for call in post.call_args_list],
                         [a['url'] for a in moderation.APIS[:3]])
        self.assertEqual(moderation.APIS[1]['name'], 'groq')
        self.assertEqual(moderation.APIS[2]['name'], 'nvidia')


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_module = load_bot()
        admin_id = patch.object(self.bot_module, 'REVIEW_CHAT_ID', '123')
        admin_id.start()
        self.addCleanup(admin_id.stop)
        self.provider = Mock(return_value='CLEAN: PASS; FLAGGED: PASS')
        provider_patch = patch.object(moderation, 'test_provider', self.provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.handler = self.bot_module.testbots_command
        self.reply = AsyncMock()
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=123, type='private'),
                                      effective_user=SimpleNamespace(id=123, is_bot=False),
                                      message=SimpleNamespace(reply_text=self.reply, sender_chat=None))
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
