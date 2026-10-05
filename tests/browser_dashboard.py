"""Optional browser smoke check. Uses temporary data/HTTPS and mocked APIs only.

Run: DASHBOARD_TEST_CHROMIUM=/path/to/chrome python tests/browser_dashboard.py
Requires the development-only playwright package.
"""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import ssl
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright, expect

from test_provider_checks import load_bot
from lib.dashboard import Dashboard, Settings, SESSION_COOKIE
from lib.dashboard_backend import BotBackend
from lib.dashboard_store import AuditStore


def tls_context(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = Path(folder) / 'cert.pem', Path(folder) / 'key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


async def main():
    module = load_bot()
    with tempfile.TemporaryDirectory() as folder, \
         patch.object(module, 'REVIEW_CHAT_ID', '-100123'), \
         patch.object(module.lib.pending, 'PENDING_FILE', str(Path(folder) / 'pending.json')), \
         patch.object(module, '_broadcast', new_callable=AsyncMock) as publish, \
         patch.object(module.lib.tally_admin, 'delete_all_submissions', return_value=2), \
         patch.object(module.lib.tracker, 'reset'):
        user = SimpleNamespace(id=10, full_name='Test Reviewer', username='reviewer', is_bot=False)
        member = SimpleNamespace(status='administrator', user=user)
        bot = SimpleNamespace(id=42, get_chat_member=AsyncMock(return_value=member),
                              get_chat=AsyncMock(return_value=SimpleNamespace(
                                  id=-100123, type='supergroup', username=None, title='ASPI review team')),
                              get_chat_administrators=AsyncMock(return_value=[member]),
                              send_message=AsyncMock(), edit_message_text=AsyncMock())
        tasks = []
        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro, **kwargs)
            tasks.append(task)
            return task
        now = time.time()
        app = SimpleNamespace(bot=bot, create_task=create_task, bot_data={
            'audit_store': AuditStore(Path(folder) / 'activity.sqlite3'),
            'runtime': {'started': now - 600, 'last_poll_attempt': now, 'last_poll_success': now,
                        'next_reset': now + 7200, 'reset_timezone': 'UTC'}})
        app.bot_data['audit_store'].record('bot_started', detail='Browser test fixture')
        module.lib.pending.save_pending('browser-test-submission',
            'My project finally works!\n<script>window.xss_hit=true</script>',
            [{'name': '<img src=x onerror=alert(1)>', 'url': 'http://169.254.169.254/secret', 'mime_type': 'image/png'}])
        module.lib.pending.update_pending('browser-test-submission', created_at=now - 120,
                                         reason='Check the wording in context before deciding.', message_id=99)
        backend = BotBackend(app, module)
        dashboard = Dashboard(Settings('https://localhost', '123', 'fake-secret'), bot, '-100123', backend)
        server = TestServer(dashboard.application())
        await server.start_server(ssl=tls_context(folder), access_log=None)
        origin = str(server.make_url('/')).rstrip('/')
        dashboard.settings = Settings(origin, '123', 'fake-secret')
        dashboard.sessions['browser-test-session'] = {'user': user, 'csrf': 'browser-test-csrf',
            'expires': now + 3600, 'idle': now + 1800, 'requests': deque()}
        try:
            async with async_playwright() as p:
                executable = os.getenv('DASHBOARD_TEST_CHROMIUM')
                browser = await p.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1000})
                    page = await context.new_page()
                    page_errors = []
                    page.on('pageerror', lambda error: page_errors.append(str(error)))
                    await page.goto(origin)
                    await expect(page.get_by_role('link', name='Continue with Telegram')).to_be_visible()
                    assert (await context.request.get(origin + '/api/reviews')).status == 401
                    await page.screenshot(path='/tmp/aspi-dashboard-login.png', full_page=True)
                    await context.add_cookies([{'name': SESSION_COOKIE, 'value': 'browser-test-session',
                                               'url': origin, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}])
                    await page.goto(origin + '/dashboard')
                    await expect(page.locator('#poll-status')).to_have_text('Polling normally')
                    await expect(page.locator('#stat-pending')).to_have_text('1')
                    await page.screenshot(path='/tmp/aspi-dashboard-desktop.png', full_page=True)
                    await page.locator('.nav-item[data-view="reviews"]').click()
                    await expect(page.locator('.submission')).to_contain_text('<script>window.xss_hit=true</script>')
                    assert not await page.evaluate('Boolean(window.xss_hit)')
                    assert await page.locator('.submission script').count() == 0
                    assert await page.locator('.attachments img').count() == 0
                    await page.screenshot(path='/tmp/aspi-dashboard-reviews.png', full_page=True)
                    page.once('dialog', lambda dialog: dialog.accept())
                    await page.get_by_role('button', name='Approve & publish').click()
                    await expect(page.locator('#review-list')).to_contain_text('The queue is clear.')
                    assert publish.await_count == 1
                    assert bot.edit_message_text.await_count == 1
                    await page.get_by_role('button', name='Activity', exact=False).click()
                    await expect(page.locator('#activity-list')).to_contain_text('Submission approved')
                    await page.get_by_role('button', name='Providers', exact=False).click()
                    await expect(page.locator('.provider-card')).to_have_count(len(module.lib.moderation.APIS))
                    async def fake_probe(context):
                        module._runtime(context)['provider_results'] = [{'name': api['name'], 'model': api['model'],
                            'result': 'CLEAN: PASS; FLAGGED: PASS (browser test fixture)'} for api in module.lib.moderation.APIS]
                        module._runtime(context)['provider_test_finished'] = time.time()
                    with patch.object(module, '_probe_providers', side_effect=fake_probe):
                        page.once('dialog', lambda dialog: dialog.accept())
                        await page.get_by_role('button', name='Test providers', exact=True).click()
                        await expect(page.locator('.provider-result').first).to_contain_text('CLEAN: PASS')
                    await page.get_by_role('button', name='Admin team', exact=False).click()
                    await expect(page.locator('#admin-list')).to_contain_text('Test Reviewer (you)')
                    await page.get_by_role('button', name='Overview', exact=True).click()
                    await expect(page.locator('#stat-pending')).to_have_text('0')
                    page.once('dialog', lambda dialog: dialog.accept('RESET'))
                    await page.get_by_role('button', name='Reset submissions…').click()
                    await expect(page.locator('#reset-status')).to_have_text('complete')
                    assert bot.send_message.await_count == 2
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    await page.locator('#toast').wait_for(state='hidden', timeout=8000)
                    await page.screenshot(path='/tmp/aspi-dashboard-mobile.png', full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not page_errors, page_errors
                    member.status = 'member'
                    await page.get_by_role('button', name='Refresh', exact=False).click()
                    await expect(page.get_by_role('link', name='Continue with Telegram')).to_be_visible()
                    print('PASS: desktop/mobile, protected APIs, safe text rendering, review/Telegram sync, providers, admins, reset, and access revocation.')
                    print('Previews: /tmp/aspi-dashboard-{login,desktop,reviews,mobile}.png')
                finally:
                    await browser.close()
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
