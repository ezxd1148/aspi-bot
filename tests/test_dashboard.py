import asyncio
from collections import deque
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import BasicAuth, ClientSession, web
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from telegram import Chat
from telegram.error import TelegramError

from test_provider_checks import load_bot
from lib.dashboard import Dashboard, Settings, ISSUER, SESSION_COOKIE, LOGIN_COOKIE
from lib.dashboard_backend import BotBackend
from lib.dashboard_store import AuditStore


class DashboardSettingsTests(unittest.TestCase):
    def test_disabled_unless_url_is_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings.from_environment())

    def test_requires_https_origin_and_login_credentials(self):
        valid = {'DASHBOARD_URL': 'https://admin.example.com', 'TELEGRAM_LOGIN_CLIENT_ID': '123',
                 'TELEGRAM_LOGIN_CLIENT_SECRET': 'secret'}
        with patch.dict(os.environ, valid, clear=True):
            self.assertEqual(Settings.from_environment().callback_url, 'https://admin.example.com/auth/callback')
        for change in ({'DASHBOARD_URL': 'http://example.com'}, {'DASHBOARD_URL': 'https://example.com/path'},
                       {'DASHBOARD_URL': 'https://example.com:invalid'}, {'TELEGRAM_LOGIN_CLIENT_ID': '0'},
                       {'DASHBOARD_URL': 'https://user:secret@example.com'}, {'DASHBOARD_PORT': '8080'},
                       {'TELEGRAM_LOGIN_CLIENT_SECRET': ''}, {'TELEGRAM_LOGIN_CLIENT_ID': 'abc'}):
            with self.subTest(change=change), patch.dict(os.environ, valid | change, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_environment()


class DashboardSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = SimpleNamespace(id=10, full_name='Admin', username='admin', is_bot=False)
        self.bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(
            status='administrator', user=self.user)))
        self.backend = SimpleNamespace(read=AsyncMock(return_value={'private': 'submission'}),
                                       action=AsyncMock(return_value={'ok': True}))
        self.dashboard = Dashboard(Settings('https://admin.example.com', '123', 'secret'),
                                   self.bot, '-100123', self.backend)
        self.client = TestClient(TestServer(self.dashboard.application()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def sign_in(self):
        self.dashboard.sessions['test-session'] = {
            'user': self.user, 'csrf': 'csrf-token', 'expires': time.time() + 3600,
            'idle': time.time() + 1800, 'requests': deque(),
        }
        return {'Cookie': SESSION_COOKIE + '=test-session'}

    async def begin_login(self):
        response = await self.client.get('/auth/login', allow_redirects=False)
        self.assertEqual(response.status, 303)
        query = parse_qs(urlsplit(response.headers['Location']).query)
        state = response.cookies[LOGIN_COOKIE].value
        return state, query, response

    async def test_public_login_has_no_submission_data_and_private_routes_deny_anonymous(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        self.assertIn('Continue with Telegram', await response.text())
        self.assertNotIn('submission-body', await response.text())
        for path in ('/api/session', '/api/overview', '/api/reviews', '/api/activity', '/api/providers', '/api/admins'):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 401)
        response = await self.client.get('/dashboard', allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.backend.read.assert_not_called()

    async def test_authorization_is_checked_again_for_every_request_and_revocation_denies(self):
        headers = self.sign_in()
        self.assertEqual((await self.client.get('/api/reviews', headers=headers)).status, 200)
        self.assertEqual((await self.client.get('/api/overview', headers=headers)).status, 200)
        self.assertEqual(self.bot.get_chat_member.await_count, 2)
        self.bot.get_chat_member.assert_awaited_with(chat_id=-100123, user_id=10)
        self.bot.get_chat_member.return_value.status = 'member'
        response = await self.client.get('/api/reviews', headers=headers)
        self.assertEqual(response.status, 403)
        self.assertNotIn('test-session', self.dashboard.sessions)
        self.assertEqual(self.backend.read.await_count, 2)

    async def test_membership_failure_and_bot_accounts_never_grant_access(self):
        headers = self.sign_in()
        self.bot.get_chat_member.side_effect = TelegramError('secret')
        response = await self.client.get('/api/reviews', headers=headers)
        self.assertEqual(response.status, 503)
        self.assertNotIn('secret', await response.text())
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value.user.is_bot = True
        self.assertEqual((await self.client.get('/api/reviews', headers=headers)).status, 403)
        self.backend.read.assert_not_called()

    async def test_expired_and_idle_sessions_are_rejected(self):
        for key in ('expires', 'idle'):
            headers = self.sign_in()
            self.dashboard.sessions['test-session'][key] = time.time() - 1
            self.assertEqual((await self.client.get('/api/reviews', headers=headers)).status, 401)
        self.bot.get_chat_member.assert_not_called()

    async def test_csrf_and_origin_are_required_for_all_mutations(self):
        headers = self.sign_in()
        for path in ('review', 'reset', 'test-providers', 'logout'):
            for extra in ({}, {'Origin': 'https://evil.example', 'X-CSRF-Token': 'csrf-token'},
                          {'Origin': 'https://admin.example.com', 'X-CSRF-Token': 'wrong'}):
                with self.subTest(path=path, extra=extra):
                    response = await self.client.post('/api/' + path, json={}, headers=headers | extra)
                    self.assertEqual(response.status, 403)
        self.backend.action.assert_not_called()
        valid = headers | {'Origin': 'https://admin.example.com', 'X-CSRF-Token': 'csrf-token'}
        response = await self.client.post('/api/review', json={'id': 'sid', 'decision': 'approve'}, headers=valid)
        self.assertEqual(response.status, 200)
        self.backend.action.assert_awaited_once_with('review', {'id': 'sid', 'decision': 'approve'}, self.user)

    async def test_json_body_size_and_type_are_bounded(self):
        headers = self.sign_in() | {'Origin': 'https://admin.example.com', 'X-CSRF-Token': 'csrf-token'}
        self.assertEqual((await self.client.post('/api/review', data='plain', headers=headers)).status, 415)
        self.assertEqual((await self.client.post('/api/review', json=[], headers=headers)).status, 400)
        self.assertEqual((await self.client.post('/api/review', json={'text': 'x' * 20000}, headers=headers)).status, 413)
        self.backend.action.assert_not_called()

    async def test_secure_headers_logout_and_session_token_invalidation(self):
        headers = self.sign_in()
        response = await self.client.get('/api/session', headers=headers)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual((await response.json())['csrf'], 'csrf-token')
        response = await self.client.post('/api/logout', json={}, headers=headers | {
            'Origin': 'https://admin.example.com', 'X-CSRF-Token': 'csrf-token'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get('/api/reviews', headers=headers)).status, 401)

    async def test_oidc_start_uses_state_nonce_pkce_and_only_profile_scope(self):
        state, query, response = await self.begin_login()
        self.assertEqual(query['state'], [state])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['redirect_uri'], ['https://admin.example.com/auth/callback'])
        self.assertEqual(query['scope'], ['openid profile'])
        self.assertIn('nonce', query)
        cookie = response.cookies[LOGIN_COOKIE]
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertNotIn('secret', response.headers['Location'])

    async def test_oidc_callback_binds_browser_state_and_rejects_replay(self):
        state, query, _ = await self.begin_login()
        with patch.object(self.dashboard, '_exchange', new_callable=AsyncMock, return_value='token') as exchange, \
             patch.object(self.dashboard, '_verify_token', new_callable=AsyncMock, return_value=self.user) as verify:
            response = await self.client.get('/auth/callback', params={'state': state, 'code': 'code'},
                                             headers={'Cookie': LOGIN_COOKIE + '=different'}, allow_redirects=False)
            self.assertEqual(response.status, 403)
            exchange.assert_not_called()
            response = await self.client.get('/auth/callback', params={'state': state, 'code': 'code'},
                                             headers={'Cookie': LOGIN_COOKIE + '=' + state}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers['Location'], '/dashboard')
            verify.assert_awaited_once_with('token', query['nonce'][0])
            cookie = response.cookies[SESSION_COOKIE]
            self.assertTrue(cookie['secure'])
            self.assertTrue(cookie['httponly'])
            self.assertEqual(cookie['path'], '/')
            response = await self.client.get('/auth/callback', params={'state': state, 'code': 'code'},
                                             headers={'Cookie': LOGIN_COOKIE + '=' + state}, allow_redirects=False)
            self.assertEqual(response.status, 403)
            exchange.assert_awaited_once()

    async def test_verified_telegram_identity_still_needs_admin_membership(self):
        state, _, _ = await self.begin_login()
        self.bot.get_chat_member.return_value.status = 'member'
        with patch.object(self.dashboard, '_exchange', new_callable=AsyncMock, return_value='token'), \
             patch.object(self.dashboard, '_verify_token', new_callable=AsyncMock, return_value=self.user):
            response = await self.client.get('/auth/callback', params={'state': state, 'code': 'code'},
                                             headers={'Cookie': LOGIN_COOKIE + '=' + state}, allow_redirects=False)
        self.assertEqual(response.status, 403)
        self.assertFalse(self.dashboard.sessions)

    async def test_real_code_exchange_and_signed_tokens_with_safe_failure_diagnostics(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        jwk.update(kid='telegram-test', use='sig')
        scenario = {}
        secret_text = 'private-code-token-secret-and-provider-description'

        async def exchange(request):
            self.assertEqual(request.headers['Authorization'], BasicAuth('123', 'secret').encode())
            form = await request.post()
            self.assertEqual(dict(form), {'grant_type': 'authorization_code', 'code': secret_text,
                                         'redirect_uri': self.dashboard.settings.callback_url,
                                         'client_id': '123', 'code_verifier': scenario['verifier']})
            if 'exchange_error' in scenario:
                return web.json_response({'error': scenario['exchange_error'], 'error_description': secret_text},
                                         status=scenario['status'])
            if scenario.get('missing_token'):
                return web.json_response({'access_token': secret_text})
            claims = {'iss': str(provider.make_url('')).rstrip('/'), 'aud': '123',
                      'sub': 'different-subject', 'id': 10, 'iat': int(time.time()),
                      'exp': int(time.time()) + 300, 'nonce': scenario['nonce'], 'name': 'Admin'}
            claims.update(scenario.get('claims', {}))
            if scenario.get('missing_nonce'):
                claims.pop('nonce')
            algorithm = scenario.get('algorithm', 'RS256')
            key = private_key if algorithm == 'RS256' else secret_text
            token = jwt.encode(claims, key, algorithm=algorithm, headers={'kid': 'telegram-test'})
            scenario['token'] = token
            return web.json_response({'id_token': token})

        async def keys(request):
            if scenario.get('keys_error'):
                return web.Response(text=secret_text, status=503)
            return web.json_response({'keys': secret_text if scenario.get('invalid_keys') else [jwk]})

        app = web.Application()
        app.add_routes([web.post('/token', exchange), web.get('/.well-known/jwks.json', keys)])
        provider = TestServer(app)
        await provider.start_server()
        self.addAsyncCleanup(provider.close)
        self.enterContext(patch('lib.dashboard.ISSUER', str(provider.make_url('')).rstrip('/')))
        async with ClientSession() as http:
            self.dashboard.http = http
            cases = [({}, None),
                     ({'exchange_error': 'invalid_client', 'status': 401}, 'token_endpoint_http_401_invalid_client'),
                     ({'exchange_error': 'invalid_grant', 'status': 400}, 'token_endpoint_http_400_invalid_grant'),
                     ({'exchange_error': secret_text, 'status': 400}, 'token_endpoint_http_400'),
                     ({'missing_token': True}, 'token_endpoint_missing_id_token'),
                     ({'missing_nonce': True}, 'missing_claim_nonce'),
                     ({'claims': {'nonce': 'wrong-☃'}}, 'nonce_mismatch'),
                     ({'claims': {'aud': 'different-client'}}, 'client_id_mismatch'),
                     ({'algorithm': 'HS256'}, 'unsupported_signing_algorithm_set_botfather_RS256'),
                     ({'keys_error': True}, 'signing_keys_http_503'),
                     ({'invalid_keys': True}, 'invalid_signing_keys_response')]
            for case, reason in cases:
                with self.subTest(case=case):
                    scenario.clear()
                    scenario.update(case)
                    self.dashboard.keys = []
                    self.dashboard.keys_until = 0
                    self.dashboard.sessions.clear()
                    state, query, _ = await self.begin_login()
                    scenario.update(nonce=query['nonce'][0], verifier=self.dashboard.logins[state]['verifier'])
                    with patch('builtins.print') as log:
                        response = await self.client.get('/auth/callback', params={'state': state, 'code': secret_text},
                                                         headers={'Cookie': LOGIN_COOKIE + '=' + state},
                                                         allow_redirects=False)
                    if reason is None:
                        self.assertEqual(response.status, 303)
                        self.assertTrue(self.dashboard.sessions)
                        log.assert_not_called()
                    else:
                        self.assertEqual(response.status, 403)
                        self.assertFalse(self.dashboard.sessions)
                        self.assertNotIn(SESSION_COOKIE, response.cookies)
                        text = await response.text()
                        self.assertIn('Check the bot service logs', text)
                        log.assert_called_once()
                        diagnostic = log.call_args.args[0]
                        self.assertIn('reason=' + reason, diagnostic)
                        stage = 'token_exchange' if reason.startswith('token_endpoint') else 'id_token_verification'
                        self.assertIn('stage=' + stage, diagnostic)
                        for sensitive in (secret_text, state, scenario['verifier'], scenario.get('token', secret_text)):
                            self.assertNotIn(sensitive, diagnostic)
                            self.assertNotIn(sensitive, text)
                        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
                    self.assertNotIn(state, self.dashboard.logins)

    async def test_connection_errors_log_only_safe_reason_not_exception_details(self):
        state, _, _ = await self.begin_login()
        with patch.object(self.dashboard, '_exchange', new_callable=AsyncMock,
                          side_effect=TimeoutError('secret-callback-code')), patch('builtins.print') as log:
            response = await self.client.get('/auth/callback', params={'state': state, 'code': 'secret-callback-code'},
                                             headers={'Cookie': LOGIN_COOKIE + '=' + state}, allow_redirects=False)
        self.assertEqual(response.status, 403)
        log.assert_called_once_with('Dashboard Telegram login failed: stage=token_exchange '
                                    'reason=telegram_request_timed_out', flush=True)
        self.assertFalse(self.dashboard.sessions)

    async def test_login_and_session_requests_are_rate_limited(self):
        self.dashboard.login_attempts.extend([time.monotonic()] * 30)
        self.assertEqual((await self.client.get('/auth/login', allow_redirects=False)).status, 429)
        headers = self.sign_in()
        self.dashboard.sessions['test-session']['requests'].extend([time.time()] * 120)
        self.assertEqual((await self.client.get('/api/reviews', headers=headers)).status, 429)

    async def test_static_routes_cannot_read_environment_or_data_files(self):
        for path in ('/.env', '/data/pending.json', '/assets/../../.env', '/assets/pending.json'):
            response = await self.client.get(path)
            self.assertNotEqual(response.status, 200)
        self.assertEqual((await self.client.get('/assets/app.css')).status, 200)


class DashboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_binds_loopback_and_closes_clients_and_sessions(self):
        dashboard = Dashboard(Settings('https://admin.example.com', '123', 'secret', port=0), None, '-100123', None)
        try:
            await dashboard.start()
            addresses = dashboard.runner.addresses
            self.assertEqual(addresses[0][0], '127.0.0.1')
            async with ClientSession() as client:
                response = await client.get(f'http://127.0.0.1:{addresses[0][1]}/')
                self.assertEqual(response.status, 200)
                self.assertIn('Continue with Telegram', await response.text())
            dashboard.sessions['test'] = {}
        finally:
            await dashboard.close()
        self.assertTrue(dashboard.http.closed)
        self.assertFalse(dashboard.sessions)


class TelegramTokenTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key(), as_dict=True)
        cls.jwk.update(kid='telegram-test', use='sig')

    def setUp(self):
        self.dashboard = Dashboard(Settings('https://admin.example.com', '123', 'secret'), None, '-100123', None)
        self.dashboard.keys = [self.jwk]
        self.dashboard.keys_until = time.time() + 3600
        self.claims = {'iss': ISSUER, 'aud': '123', 'sub': 'different-from-telegram-id', 'id': 10,
                       'iat': int(time.time()), 'exp': int(time.time()) + 300, 'nonce': 'nonce',
                       'name': 'Admin', 'preferred_username': 'reviewer'}

    def encode(self, changes=None):
        return jwt.encode(self.claims | (changes or {}), self.private_key, algorithm='RS256', headers={'kid': 'telegram-test'})

    async def test_valid_signature_returns_telegram_id_not_oidc_subject(self):
        user = await self.dashboard._verify_token(self.encode(), 'nonce')
        self.assertEqual(user.id, 10)
        self.assertEqual(user.full_name, 'Admin')

    async def test_signature_issuer_audience_expiry_nonce_and_freshness_are_enforced(self):
        for changes in ({'iss': 'https://evil.example'}, {'aud': '999'}, {'exp': int(time.time()) - 5},
                        {'nonce': 'wrong'}, {'iat': int(time.time()) - 600}, {'iat': int(time.time()) + 600},
                        {'id': '10'}, {'id': True}, {'id': -1}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, jwt.PyJWTError)):
                await self.dashboard._verify_token(self.encode(changes), 'nonce')
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        invalid = jwt.encode(self.claims, other_key, algorithm='RS256', headers={'kid': 'telegram-test'})
        with self.assertRaises(jwt.InvalidSignatureError):
            await self.dashboard._verify_token(invalid, 'nonce')

    async def test_unsigned_wrong_algorithm_and_missing_claims_are_denied(self):
        wrong = jwt.encode(self.claims, 'x' * 40, algorithm='HS256', headers={'kid': 'telegram-test'})
        with self.assertRaises(ValueError):
            await self.dashboard._verify_token(wrong, 'nonce')
        for field in ('iss', 'aud', 'exp', 'iat', 'id', 'nonce', 'sub'):
            claims = self.claims.copy()
            claims.pop(field)
            token = jwt.encode(claims, self.private_key, algorithm='RS256', headers={'kid': 'telegram-test'})
            with self.subTest(field=field), self.assertRaises(jwt.MissingRequiredClaimError):
                await self.dashboard._verify_token(token, 'nonce')


class DashboardBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_bot()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target, name, value in ((self.module, 'REVIEW_CHAT_ID', '-100123'),
                                   (self.module.lib.pending, 'PENDING_FILE', str(Path(self.folder.name) / 'pending.json'))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.store = AuditStore(Path(self.folder.name) / 'activity.sqlite3')
        self.user = SimpleNamespace(id=10, full_name='Admin <script>', username='reviewer', is_bot=False)
        self.tasks = []
        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro, **kwargs)
            self.tasks.append(task)
            return task
        self.bot = SimpleNamespace(id=42, get_chat_member=AsyncMock(return_value=SimpleNamespace(
            status='administrator', user=self.user)),
            get_chat=AsyncMock(return_value=SimpleNamespace(id=-100123, title='Review group', type='supergroup', username=None)),
            get_chat_administrators=AsyncMock(return_value=[SimpleNamespace(status='administrator', user=self.user)]),
            send_message=AsyncMock(), edit_message_text=AsyncMock())
        self.app = SimpleNamespace(bot=self.bot, bot_data={'audit_store': self.store}, create_task=create_task)
        self.backend = BotBackend(self.app, self.module)
        self.broadcast = AsyncMock()
        p = patch.object(self.module, '_broadcast', self.broadcast)
        p.start()
        self.addCleanup(p.stop)
        self.module.lib.pending.save_pending('sid', '<script>test</script>', [
            {'name': '<img onerror=alert(1)>', 'url': 'http://169.254.169.254/secret', 'mime_type': 'text/html'}])
        self.module.lib.pending.update_pending('sid', reason='Borderline wording', message_id=99, created_at=time.time())

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_reviews_include_metadata_and_telegram_link_without_untrusted_download_urls(self):
        data = await self.backend.read('reviews', {})
        self.assertEqual(data['total'], 1)
        item = data['items'][0]
        self.assertEqual(item['reason'], 'Borderline wording')
        self.assertEqual(item['text'], '<script>test</script>')
        self.assertEqual(item['telegram_url'], 'https://t.me/c/123/99')
        self.assertNotIn('url', item['files'][0])
        self.assertEqual(item['files'][0]['name'], '<img onerror=alert(1)>')
        for query in ({'limit': '500'}, {'offset': '-1'}, {'offset': 'invalid'}):
            with self.assertRaises(web.HTTPBadRequest):
                await self.backend.read('reviews', query)

    async def test_telegram_and_dashboard_decisions_share_lock_and_publish_once(self):
        async def publishing(*args):
            await asyncio.sleep(0)
        self.broadcast.side_effect = publishing
        query = SimpleNamespace(data='ok_sid', answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=-100123, type='supergroup'),
                                 effective_user=self.user, message=None, callback_query=query)
        results = await asyncio.gather(
            self.backend.action('review', {'id': 'sid', 'decision': 'approve'}, self.user),
            self.module.button_handler(update, self.backend.context), return_exceptions=True)
        self.broadcast.assert_awaited_once()
        self.assertIsNone(self.module.lib.pending.get_pending('sid'))
        self.assertEqual(self.store.counts(0).get('approved'), 1)
        self.assertTrue(any(not isinstance(result, BaseException) for result in results))

    async def test_web_decision_updates_telegram_and_edit_failure_cannot_republish(self):
        self.bot.edit_message_text.side_effect = TelegramError('secret')
        data = await self.backend.action('review', {'id': 'sid', 'decision': 'approve'}, self.user)
        self.assertTrue(data['ok'])
        text = self.bot.edit_message_text.call_args.kwargs['text']
        self.assertIn('Admin &lt;script&gt;', text)
        self.assertIn('&lt;script&gt;test&lt;/script&gt;', text)
        with self.assertRaises(web.HTTPConflict):
            await self.backend.action('review', {'id': 'sid', 'decision': 'approve'}, self.user)
        self.broadcast.assert_awaited_once()

    async def test_failed_publish_retains_pending_and_records_safe_failure(self):
        self.broadcast.side_effect = TelegramError('secret')
        with self.assertRaises(web.HTTPBadGateway):
            await self.backend.action('review', {'id': 'sid', 'decision': 'approve'}, self.user)
        self.assertIsNotNone(self.module.lib.pending.get_pending('sid'))
        self.bot.edit_message_text.assert_not_called()
        self.assertEqual(self.store.recent()[0]['detail'], 'TelegramError')

    async def test_reject_and_invalid_actions_never_publish(self):
        for body in ({'id': 'sid', 'decision': 'delete'}, {'id': [], 'decision': 'approve'},
                     {'id': 'sid', 'decision': []}, {'decision': 'approve'}):
            with self.assertRaises(web.HTTPBadRequest):
                await self.backend.action('review', body, self.user)
        await self.backend.action('review', {'id': 'sid', 'decision': 'reject'}, self.user)
        self.broadcast.assert_not_called()
        self.assertEqual(self.store.recent()[0]['source'], 'dashboard')

    async def test_reset_requires_confirmation_and_retains_audit_history(self):
        with patch.object(self.module.lib.tally_admin, 'delete_all_submissions', return_value=1) as delete, \
             patch.object(self.module.lib.tracker, 'reset'):
            with self.assertRaises(web.HTTPBadRequest):
                await self.backend.action('reset', {}, self.user)
            delete.assert_not_called()
            await self.backend.action('reset', {'confirmation': 'RESET'}, self.user)
        self.assertEqual(self.module.lib.pending.list_pending(), {})
        self.assertEqual(self.store.counts(0)['reset_complete'], 1)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn('Dashboard reset starting', self.bot.send_message.call_args_list[0].kwargs['text'])

    async def test_failed_reset_preserves_reviews_and_is_visible_in_overview(self):
        with patch.object(self.module.lib.tally_admin, 'delete_all_submissions', side_effect=RuntimeError('secret')):
            with self.assertRaises(web.HTTPBadGateway):
                await self.backend.action('reset', {'confirmation': 'RESET'}, self.user)
        overview = await self.backend.read('overview', {})
        self.assertEqual(overview['pending'], 1)
        self.assertEqual(overview['last_reset']['status'], 'failed')
        self.assertFalse(overview['reset_running'])
        self.assertNotIn('secret', str(overview))

    async def test_provider_probe_guard_is_shared_with_telegram_and_released(self):
        self.app.bot_data['provider_test_running'] = True
        with self.assertRaises(web.HTTPConflict):
            await self.backend.action('test-providers', {}, self.user)
        self.app.bot_data.pop('provider_test_running')
        with patch.object(self.module, '_probe_providers', new_callable=AsyncMock) as probe:
            response = await self.backend.action('test-providers', {}, self.user)
            self.assertTrue(response['ok'])
            self.assertTrue(self.app.bot_data['provider_test_running'])
            await asyncio.gather(*self.tasks)
            probe.assert_awaited_once()
        self.assertNotIn('provider_test_running', self.app.bot_data)

    async def test_provider_views_never_return_keys_or_request_bodies(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'super-secret-key'}):
            data = await self.backend.read('providers', {})
        self.assertTrue(data['items'][0]['configured'])
        self.assertNotIn('super-secret-key', str(data))

    async def test_legacy_private_admin_view_uses_chat_identity(self):
        with patch.object(self.module, 'REVIEW_CHAT_ID', '10'):
            self.bot.get_chat.return_value = Chat(id=10, type='private', first_name='Legacy Admin', username='legacy')
            data = await self.backend.read('admins', {})
        self.assertEqual(data['items'], [{'id': 10, 'name': 'Legacy Admin', 'username': 'legacy', 'role': 'Private admin'}])
        self.bot.get_chat_administrators.assert_not_called()

    async def test_overview_does_not_report_healthy_before_successful_poll(self):
        self.assertFalse((await self.backend.read('overview', {}))['poll_healthy'])
        self.module._runtime(self.backend.context)['last_poll_success'] = time.time()
        self.assertTrue((await self.backend.read('overview', {}))['poll_healthy'])
        self.module._runtime(self.backend.context)['last_poll_error'] = 'Timeout'
        self.assertFalse((await self.backend.read('overview', {}))['poll_healthy'])


class AuditAndPendingTests(unittest.TestCase):
    def test_audit_survives_restart_and_records_only_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.sqlite3'
            store = AuditStore(path)
            store.record('approved', submission_id='sid', actor=SimpleNamespace(id=10, full_name='Admin'), source='dashboard')
            reopened = AuditStore(path)
            self.assertEqual(reopened.counts(0), {'approved': 1})
            self.assertEqual(reopened.recent()[0]['actor_id'], 10)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('text', reopened.recent()[0])

    def test_audit_retention_removes_old_events(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AuditStore(Path(folder) / 'audit.sqlite3')
            with patch('lib.dashboard_store.time.time', return_value=time.time() - 31 * 86400):
                store.record('old')
            store.record('new')
            self.assertEqual([row['kind'] for row in store.recent()], ['new'])

    def test_legacy_pending_entries_preserve_content_when_metadata_is_added(self):
        from lib import pending
        with tempfile.TemporaryDirectory() as folder, patch.object(pending, 'PENDING_FILE', str(Path(folder) / 'pending.json')):
            pending._save({'sid': 'legacy text'})
            pending.update_pending('sid', reason='Review note')
            self.assertEqual(pending.get_pending('sid'), {'text': 'legacy text', 'files': [], 'reason': 'Review note'})
