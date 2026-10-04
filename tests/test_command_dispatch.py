import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.error import TelegramError
from telegram.ext import CallbackContext, CommandHandler
from test_provider_checks import load_bot


class CommandDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_bot()
        target = patch.object(self.bot, 'REVIEW_CHAT_ID', '-100123')
        target.start()
        self.addCleanup(target.stop)
        self.context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(
            id=42, username='ReviewBot',
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')),
            get_chat=AsyncMock(return_value=SimpleNamespace(
                id=-100123, type='supergroup', username=None, title='Reviewers')),
            set_my_commands=AsyncMock(),
        ))
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100123, type='supergroup'),
            effective_user=SimpleNamespace(id=10, is_bot=False, full_name='Reviewer'),
            message=SimpleNamespace(reply_text=AsyncMock(), sender_chat=None, text='/status'),
        )

    async def test_start_shows_loaded_destination_and_build(self):
        await self.bot.start_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('-100123', text)
        self.assertIn(self.bot.BUILD_ID, text)
        self.assertIn('ADMIN_GROUP_ID', text)

    async def test_status_reports_correct_loaded_destination(self):
        await self.bot.status_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('Review chat: -100123', text)
        self.assertIn('PASS', text)
        self.assertIn('/testbots', text)

    async def test_startup_rejects_invalid_destination_or_permissions(self):
        for changes in ({'type': 'private'}, {'username': 'publicgroup'}, {'id': -100999}):
            with self.subTest(changes=changes):
                self.context.bot.get_chat.return_value = SimpleNamespace(
                    **({'id': -100123, 'type': 'supergroup', 'username': None} | changes))
                with self.assertRaises(RuntimeError):
                    await self.bot._validate_review_destination(self.context.bot)
        self.context.bot.get_chat.return_value = SimpleNamespace(id=-100123, type='supergroup', username=None)
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status='member')
        with self.assertRaises(RuntimeError):
            await self.bot._validate_review_destination(self.context.bot)

    async def test_bad_group_stops_before_menu_or_tracker_work(self):
        self.context.bot.get_chat.side_effect = TelegramError('not found')
        with patch.object(self.bot, '_seed_tracker') as seed, self.assertRaises(TelegramError):
            await self.bot._post_init(self.context)
        seed.assert_not_called()
        self.context.bot.set_my_commands.assert_not_called()

    async def test_startup_menu_and_dispatch_share_registry(self):
        with patch.object(self.bot, '_seed_tracker'), patch.object(self.bot.lib.web_server, 'start'):
            await self.bot._post_init(self.context)
        calls = self.context.bot.set_my_commands.call_args_list
        self.assertEqual([c.command for c in calls[0].args[0]], ['start', 'help'])
        self.assertEqual([c.command for c in calls[1].args[0]], [c[0] for c in self.bot.COMMANDS])
        self.assertEqual(calls[1].kwargs['scope'].chat_id, -100123)

    async def test_help_and_unknown_commands_respond(self):
        await self.bot.help_command(self.update, self.context)
        for name, *_ in self.bot.COMMANDS:
            self.assertIn('/' + name, self.update.message.reply_text.call_args.args[0])
        self.update.message.text = '/typo'
        await self.bot.unknown_command(self.update, self.context)
        self.assertIn('Unknown command', self.update.message.reply_text.call_args.args[0])
        self.update.message.reply_text.reset_mock()
        self.update.message.text = '/status@AnotherBot'
        await self.bot.unknown_command(self.update, self.context)
        self.update.message.reply_text.assert_not_called()

    async def test_reset_success_and_failure_are_truthful(self):
        with patch.object(self.bot.lib.tally_admin, 'delete_all_submissions', return_value=2) as delete, \
             patch.object(self.bot.lib.tracker, 'reset') as tracker, \
             patch.object(self.bot.lib.pending, 'clear_all') as pending:
            await self.bot.reset_command(self.update, self.context)
            delete.assert_called_once()
            tracker.assert_called_once()
            pending.assert_called_once()
            tracker.reset_mock()
            pending.reset_mock()
            delete.side_effect = RuntimeError('Tally unavailable')
            await self.bot.reset_command(self.update, self.context)
            tracker.assert_not_called()
            pending.assert_not_called()
            self.assertIn('preserved', self.update.message.reply_text.call_args.args[0])
            await self.bot.daily_reset(self.context)
            tracker.assert_not_called()
            pending.assert_not_called()

    async def test_unexpected_provider_failure_does_not_hide_other_results(self):
        def probe(api):
            if api['name'] == 'openrouter':
                raise RuntimeError('secret')
            return 'CLEAN: PASS; FLAGGED: PASS'
        with patch.object(self.bot.lib.moderation, 'test_provider', side_effect=probe):
            await self.bot.testbots_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('test error (RuntimeError)', text)
        self.assertIn('groq', text)
        self.assertIn('CLEAN: PASS', text)
        self.assertNotIn('secret', text)

    async def test_actual_telegram_updates_dispatch_every_registered_command(self):
        app = self.bot.create_application()
        try:
            # Avoid network initialization; all handler behavior is covered separately.
            app._initialized = True
            fake_bot = SimpleNamespace(username='ReviewBot')
            errors = AsyncMock()
            for name, *_ in self.bot.COMMANDS:
                handler = next(h for h in app.handlers[0]
                               if isinstance(h, CommandHandler) and name in h.commands)
                with self.subTest(command=name):
                    text = '/' + name + '@ReviewBot'
                    message = Message(
                        message_id=1, date=datetime.now(timezone.utc),
                        chat=Chat(id=-100123, type='supergroup'), from_user=User(id=10, first_name='Admin', is_bot=False),
                        text=text, entities=[MessageEntity(type='bot_command', offset=0, length=len(text))],
                    )
                    message.set_bot(fake_bot)
                    update = Update(update_id=1, message=message)
                    called = AsyncMock()
                    with patch.object(handler, 'callback', called), \
                         patch.object(CallbackContext, 'refresh_data', new_callable=AsyncMock), \
                         patch.object(type(app), 'process_error', errors), \
                         patch.object(handler, 'block', True):
                        await app.process_update(update)
                    called.assert_awaited_once()
            errors.assert_not_called()
        finally:
            app._initialized = False
            if app.job_queue:
                await app.job_queue.stop(wait=False)
