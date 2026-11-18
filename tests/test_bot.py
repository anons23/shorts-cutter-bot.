"""Тесты логики бота без Telegram (нужен aiogram): python -m unittest discover tests"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

try:
    os.environ.setdefault("STATE_FILE", os.devnull)
    import bot
except ImportError:  # aiogram не установлен — пропускаем
    bot = None


def message(uid: int = 1, text: str | None = None):
    m = MagicMock()
    m.from_user.id = uid
    m.text = text
    m.answer = AsyncMock()
    return m


def callback(uid: int, data: str):
    c = MagicMock()
    c.from_user.id = uid
    c.data = data
    c.answer = AsyncMock()
    c.message.edit_reply_markup = AsyncMock()
    c.message.answer = AsyncMock()
    return c


@unittest.skipIf(bot is None, "aiogram не установлен")
class BotFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.drafts.clear()
        bot.pending.clear()
        bot.usage.clear()
        bot.queue = asyncio.Queue()

    def queued(self):
        items = []
        while not bot.queue.empty():
            items.append(bot.queue.get_nowait())
        return items

    async def test_link_with_timecodes_goes_straight_to_queue(self):
        text = "https://youtu.be/abc 1:20-1:45, 5:10-5:40"
        await bot.on_link(message(text=text))
        (_, kind, payload, ranges), = self.queued()
        self.assertEqual(kind, "youtube")
        self.assertEqual(payload, "https://youtu.be/abc")
        self.assertEqual(ranges, [(80, 105), (310, 340)])

    async def test_link_without_timecodes_asks_mode(self):
        m = message(text="https://youtu.be/abc")
        await bot.on_link(m)
        self.assertEqual(self.queued(), [])
        self.assertIn(1, bot.drafts)
        self.assertIsNotNone(m.answer.call_args.kwargs.get("reply_markup"))

    async def test_manual_mode_then_timecodes(self):
        await bot.on_link(message(text="https://youtu.be/abc"))
        c = callback(1, "mode:manual")
        await bot.mode_cb(c)
        c.message.answer.assert_called_once()
        await bot.on_timecodes(message(text="0:10-0:30\n1:00-1:20"))
        (_, _, _, ranges), = self.queued()
        self.assertEqual(ranges, [(10, 30), (60, 80)])
        self.assertNotIn(1, bot.drafts)

    async def test_auto_mode(self):
        await bot.on_link(message(text="https://youtu.be/abc"))
        await bot.mode_cb(callback(1, "mode:auto"))
        (_, kind, _, ranges), = self.queued()
        self.assertEqual(kind, "youtube")
        self.assertIsNone(ranges)

    async def test_timecodes_without_video(self):
        m = message(text="1:00-1:20")
        await bot.on_timecodes(m)
        self.assertEqual(self.queued(), [])
        self.assertIn("Сначала", m.answer.call_args.args[0])

    async def test_daily_limit_counts_queue(self):
        bot.DAILY_LIMIT = 3
        for _ in range(2):
            await bot.on_link(message(text="https://youtu.be/abc 0:10-0:20"))
        m = message(text="https://youtu.be/abc 0:10-0:20")
        await bot.on_link(m)  # третье — уже больше 2 в очереди одного человека
        self.assertEqual(len(self.queued()), 2)
        self.assertIn("Дождись", m.answer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
