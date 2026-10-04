import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.error import Conflict, TelegramError
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
            get_chat_administrators=AsyncMock(return_value=[
                SimpleNamespace(status='creator', user=User(id=9, first_name='Group Owner', is_bot=False)),
                SimpleNamespace(status='administrator', user=User(id=10, first_name='Reviewer',
                                                                  username='reviewer', is_bot=False)),
                SimpleNamespace(status='administrator', user=User(id=42, first_name='Review Bot', is_bot=True)),
            ]),
            set_my_commands=AsyncMock(),
            send_message=AsyncMock(),
        ))
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100123, type='supergroup'),
            effective_user=SimpleNamespace(id=10, is_bot=False, full_name='Reviewer', username='reviewer'),
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
        self.assertIn('Detected admins (2):', text)
        self.assertIn('Group Owner (ID: 9) - Owner', text)
        self.assertIn('Reviewer @reviewer (ID: 10) - Administrator (you)', text)
        self.assertIn('Requested by: Reviewer @reviewer (ID: 10) - Administrator', text)
        self.assertNotIn('Review Bot', text)
        self.context.bot.get_chat_administrators.assert_awaited_once_with(chat_id=-100123)

    async def test_status_detects_requesting_owner(self):
        self.update.effective_user = User(id=9, first_name='Group Owner', is_bot=False)
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status='creator')
        await self.bot.status_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('Requested by: Group Owner (ID: 9) - Owner', text)
        self.assertIn('Group Owner (ID: 9) - Owner (you)', text)

    async def test_status_keeps_diagnostics_when_admin_list_lookup_fails(self):
        self.context.bot.get_chat_administrators.side_effect = TelegramError('secret')
        await self.bot.status_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('Review chat: -100123', text)
        self.assertIn('Detected admins: lookup failed (Telegram TelegramError)', text)
        self.assertIn('Group admin (verified)', text)
        self.assertNotIn('secret', text)

    async def test_status_does_not_disclose_admin_list_to_regular_members(self):
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status='member')
        await self.bot.status_command(self.update, self.context)
        self.assertIn('Admin only', self.update.message.reply_text.call_args.args[0])
        self.context.bot.get_chat_administrators.assert_not_awaited()

    async def test_status_private_mode_detects_configured_admin(self):
        with patch.object(self.bot, 'REVIEW_CHAT_ID', '10'):
            self.update.effective_chat = SimpleNamespace(id=10, type='private')
            self.context.bot.get_chat.return_value = SimpleNamespace(id=10, type='private', title=None)
            await self.bot.status_command(self.update, self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('Detected admins (1):', text)
        self.assertIn('Reviewer @reviewer (ID: 10) - Private admin (you)', text)
        self.context.bot.get_chat_administrators.assert_not_awaited()

    async def test_status_splits_large_admin_list_without_losing_members(self):
        self.context.bot.get_chat_administrators.return_value = [
            SimpleNamespace(status='administrator', user=User(
                id=1000 + number, first_name='Reviewer ' + str(number) + 'x' * 50, is_bot=False))
            for number in range(100)
        ]
        await self.bot.status_command(self.update, self.context)
        pages = [call.args[0] for call in self.update.message.reply_text.call_args_list]
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 3500 for page in pages))
        text = '\n'.join(pages)
        for number in range(100):
            self.assertIn(f'(ID: {1000 + number}) - Administrator', text)

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

    async def test_polling_conflict_reports_competing_process_without_secrets(self):
        self.context.error = Conflict('terminated by other getUpdates request secret')
        with patch('builtins.print') as log:
            await self.bot._error_handler(None, self.context)
        text = ' '.join(call.args[0] for call in log.call_args_list)
        self.assertIn('another process', text)
        self.assertIn('aspi-bot-ec2.service', text)
        self.assertNotIn('secret', text)

    async def test_webhook_conflict_is_reported_separately(self):
        self.context.error = Conflict('cannot use getUpdates while webhook is active')
        with patch('builtins.print') as log:
            await self.bot._error_handler(None, self.context)
        text = ' '.join(call.args[0] for call in log.call_args_list)
        self.assertIn('polling/webhook conflict', text)

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
            notices = [call.kwargs['text'] for call in self.context.bot.send_message.call_args_list]
            self.assertEqual(len(notices), 2)
            self.assertIn('Daily reset starting', notices[0])
            self.assertIn('Local pending reviews were preserved', notices[1])

    async def test_daily_reset_notifies_review_chat_before_deletion_and_after_completion(self):
        def delete_submissions(*args):
            self.context.bot.send_message.assert_awaited_once()
            self.assertIn('Daily reset starting', self.context.bot.send_message.call_args.kwargs['text'])
            return 2

        with patch.object(self.bot.lib.tally_admin, 'delete_all_submissions', side_effect=delete_submissions) as delete, \
             patch.object(self.bot.lib.tracker, 'reset') as tracker, \
             patch.object(self.bot.lib.pending, 'clear_all') as pending:
            await self.bot.daily_reset(self.context)
            delete.assert_called_once()
            tracker.assert_called_once()
            pending.assert_called_once()
        notices = self.context.bot.send_message.call_args_list
        self.assertEqual(len(notices), 2)
        self.assertIn('Daily reset complete', notices[1].kwargs['text'])
        for call in notices:
            self.assertEqual(call.kwargs['chat_id'], '-100123')
            self.assertIsNone(call.kwargs['parse_mode'])

    async def test_daily_reset_runs_when_telegram_notifications_fail(self):
        self.context.bot.send_message.side_effect = TelegramError('secret')
        with patch.object(self.bot.lib.tally_admin, 'delete_all_submissions', return_value=0) as delete, \
             patch.object(self.bot.lib.tracker, 'reset') as tracker, \
             patch.object(self.bot.lib.pending, 'clear_all') as pending, patch('builtins.print') as log:
            await self.bot.daily_reset(self.context)
        delete.assert_called_once()
        tracker.assert_called_once()
        pending.assert_called_once()
        self.assertEqual(self.context.bot.send_message.await_count, 2)
        self.assertNotIn('secret', ' '.join(call.args[0] for call in log.call_args_list))

    async def test_daily_reset_local_failure_never_reports_completion(self):
        with patch.object(self.bot.lib.tally_admin, 'delete_all_submissions', return_value=2), \
             patch.object(self.bot.lib.tracker, 'reset', side_effect=OSError('secret')), \
             patch.object(self.bot.lib.pending, 'clear_all') as pending:
            await self.bot.daily_reset(self.context)
        pending.assert_not_called()
        notices = [call.kwargs['text'] for call in self.context.bot.send_message.call_args_list]
        self.assertEqual(len(notices), 2)
        self.assertIn('Tally was cleared', notices[1])
        self.assertNotIn('complete', notices[1])
        self.assertNotIn('secret', notices[1])

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
