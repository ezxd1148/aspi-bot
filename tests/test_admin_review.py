import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import TelegramError
from test_provider_checks import load_bot


class AdminReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_bot()
        config = patch.object(self.bot, 'REVIEW_CHAT_ID', '-100123')
        config.start()
        self.addCleanup(config.stop)
        self.context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
            send_media_group=AsyncMock(),
        ))
        self.update = self.make_update()
        self.pending = {'sid': {'text': '<harmless & text>', 'files': []}}
        self.real_broadcast = self.bot._broadcast
        self.broadcast = AsyncMock()
        patches = [
            patch.object(self.bot, '_broadcast', self.broadcast),
            patch.object(self.bot.lib.pending, 'get_pending', side_effect=self.pending.get),
            patch.object(self.bot.lib.pending, 'remove_pending', side_effect=lambda sid: self.pending.pop(sid, None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_update(self, user_id=1, data='ok_sid'):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100123, type='supergroup'),
            effective_user=SimpleNamespace(id=user_id, full_name=f'Admin <{user_id}>', is_bot=False),
            message=None,
            callback_query=SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()),
        )

    async def test_owner_and_administrator_allowed_other_roles_denied(self):
        for status in ('creator', 'administrator', 'member', 'restricted', 'left', 'kicked'):
            self.context.bot.get_chat_member.return_value = SimpleNamespace(status=status)
            self.assertEqual(await self.bot._is_admin(self.update, self.context),
                             status in ('creator', 'administrator'))

    async def test_wrong_chat_and_bot_identity_cannot_review(self):
        self.update.effective_chat.id = -100999
        self.assertFalse(await self.bot._is_admin(self.update, self.context))
        self.context.bot.get_chat_member.assert_not_called()
        self.update.effective_chat.id = -100123
        self.update.effective_user.is_bot = True
        self.assertFalse(await self.bot._is_admin(self.update, self.context))

    async def test_membership_lookup_failure_denies_access(self):
        self.context.bot.get_chat_member.side_effect = TelegramError('unavailable')
        self.assertFalse(await self.bot._is_admin(self.update, self.context))

    async def test_anonymous_command_is_denied(self):
        self.update.message = SimpleNamespace(sender_chat=SimpleNamespace(id=-100123))
        self.assertFalse(await self.bot._is_admin(self.update, self.context))

    async def test_removed_admin_cannot_approve_or_run_commands(self):
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status='member')
        await self.bot.button_handler(self.update, self.context)
        self.broadcast.assert_not_called()
        self.assertIn('sid', self.pending)
        self.update.message = SimpleNamespace(sender_chat=None, reply_text=AsyncMock())
        with patch.object(self.bot.lib.moderation, 'test_provider') as probe, \
             patch.object(self.bot.lib.tally_admin, 'delete_all_submissions') as reset:
            await self.bot.testbots_command(self.update, self.context)
            await self.bot.reset_command(self.update, self.context)
            probe.assert_not_called()
            reset.assert_not_called()

    async def test_two_admin_clicks_publish_once_and_preserve_first_decision(self):
        async def publish(*args):
            await asyncio.sleep(0)
        self.broadcast.side_effect = publish
        second = self.make_update(user_id=2)
        await asyncio.gather(self.bot.button_handler(self.update, self.context),
                             self.bot.button_handler(second, self.context))
        self.broadcast.assert_awaited_once()
        self.assertNotIn('sid', self.pending)
        self.assertEqual(self.update.callback_query.edit_message_text.await_count +
                         second.callback_query.edit_message_text.await_count, 1)
        label = self.update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Admin &lt;1&gt;', label)
        self.assertIn('&lt;harmless &amp; text&gt;', label)

    async def test_failed_display_edit_cannot_allow_republication(self):
        self.update.callback_query.edit_message_text.side_effect = TelegramError('cannot edit')
        await self.bot.button_handler(self.update, self.context)
        await self.bot.button_handler(self.make_update(user_id=2), self.context)
        self.broadcast.assert_awaited_once()
        self.assertNotIn('sid', self.pending)

    async def test_failed_publish_retains_pending_and_buttons_for_retry(self):
        self.broadcast.side_effect = TelegramError('cannot post')
        await self.bot.button_handler(self.update, self.context)
        self.assertIn('sid', self.pending)
        self.update.callback_query.edit_message_text.assert_not_called()
        self.context.bot.send_message.assert_awaited_once()
        self.broadcast.side_effect = None
        await self.bot.button_handler(self.make_update(user_id=2), self.context)
        self.assertNotIn('sid', self.pending)

    async def test_rejection_never_publishes(self):
        self.update.callback_query.data = 'no_sid'
        await self.bot.button_handler(self.update, self.context)
        self.broadcast.assert_not_called()
        self.assertNotIn('sid', self.pending)
        self.assertIn('Rejected', self.update.callback_query.edit_message_text.call_args.args[0])

    async def test_invalid_callback_leaves_pending_untouched(self):
        for data in ('unexpected', 'delete_sid', None):
            self.update.callback_query.data = data
            await self.bot.button_handler(self.update, self.context)
        self.broadcast.assert_not_called()
        self.assertIn('sid', self.pending)

    async def test_flagged_submission_and_note_go_to_review_group(self):
        with patch.object(self.bot.lib.moderation, 'moderate_submission',
                          return_value={'result': 'flagged', 'reason': 'Possibly <coded> wording.'}), \
             patch.object(self.bot.lib.pending, 'save_pending') as save, \
             patch.object(self.bot.lib.tracker, 'mark_processed') as processed, \
             patch.object(self.bot, '_send_files', new_callable=AsyncMock) as files:
            await self.bot._handle_submission(self.context, 'new', '<hello>', [])
            sent = self.context.bot.send_message.call_args.kwargs
            self.assertEqual(sent['chat_id'], '-100123')
            self.assertIn('&lt;hello&gt;', sent['text'])
            self.assertIn('Review note:</b> Possibly &lt;coded&gt; wording.', sent['text'])
            save.assert_called_once_with('new', '<hello>', [])
            processed.assert_called_once_with('new')
            files.assert_awaited_once()
        self.broadcast.assert_not_called()

    async def test_obvious_flag_omits_note(self):
        with patch.object(self.bot.lib.moderation, 'moderate_submission',
                          return_value={'result': 'flagged', 'reason': ''}), \
             patch.object(self.bot.lib.pending, 'save_pending'), \
             patch.object(self.bot.lib.tracker, 'mark_processed'):
            await self.bot._handle_submission(self.context, 'new', 'test', [])
        self.assertNotIn('Review note', self.context.bot.send_message.call_args.kwargs['text'])

    async def test_clean_submission_is_published_without_review(self):
        with patch.object(self.bot.lib.moderation, 'moderate_submission',
                          return_value={'result': 'clean', 'reason': ''}), \
             patch.object(self.bot.lib.tracker, 'mark_processed'):
            await self.bot._handle_submission(self.context, 'new', 'hello', [])
        self.broadcast.assert_awaited_once()
        self.context.bot.send_message.assert_not_called()

    async def test_media_send_failure_propagates_to_review_handler(self):
        self.context.bot.send_media_group.side_effect = TelegramError('cannot send media')
        with self.assertRaises(TelegramError):
            await self.real_broadcast(self.context.bot, 'hello', [
                {'mime_type': 'image/jpeg', 'url': 'https://example.com/photo.jpg'}
            ])
