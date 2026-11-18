"""Тесты логики бота без Telegram (нужен aiogram): python -m unittest discover tests"""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ["DB_PATH"] = ":memory:"
os.environ["WEBAPP_URL"] = "https://example.github.io/timecodes.html"
try:
    import bot
    import storage
except ImportError:  # aiogram не установлен — пропускаем
    bot = None

UID = 1


def tg_bot():
    b = MagicMock()
    b.me = AsyncMock(return_value=SimpleNamespace(username="test_bot"))
    b.send_message = AsyncMock()
    b.download = AsyncMock()
    return b


def message(uid: int = UID, text: str | None = None, web_app_data: str | None = None):
    m = MagicMock()
    m.from_user.id = uid
    m.text = text
    m.web_app_data = SimpleNamespace(data=web_app_data) if web_app_data else None
    status = MagicMock()
    status.edit_text = AsyncMock()
    m.answer = AsyncMock(return_value=status)
    m.answer_video = AsyncMock()
    m.edit_reply_markup = AsyncMock()
    m.bot = tg_bot()
    return m


def callback(data: str, uid: int = UID):
    c = MagicMock()
    c.from_user.id = uid
    c.data = data
    c.answer = AsyncMock()
    c.message = message(uid)
    return c


def texts(m) -> str:
    return "\n".join(str(call.args[0]) for call in m.answer.call_args_list if call.args)


@unittest.skipIf(bot is None, "aiogram не установлен")
class BotFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.drafts.clear()
        bot.pending.clear()
        bot.queue = asyncio.Queue()
        bot.ADMIN_IDS = set()
        bot.FREE_FRAGMENTS_PER_DAY = 5
        storage._db.execute("DELETE FROM users")
        storage._db.execute("DELETE FROM usage")

    def queued(self):
        items = []
        while not bot.queue.empty():
            items.append(bot.queue.get_nowait())
        return items

    async def test_link_with_timecodes_goes_straight_to_queue(self):
        await bot.on_link(message(text="https://youtu.be/dQw4w9WgXcQ 1:20-1:45, 5:10-5:40"))
        (_, kind, payload, ranges, charge), = self.queued()
        self.assertEqual((kind, payload), ("youtube", "https://youtu.be/dQw4w9WgXcQ"))
        self.assertEqual(ranges, [(80, 105), (310, 340)])
        self.assertEqual(storage.used_today(UID)["fragments"], 2)

    async def test_step_by_step_in_chat(self):
        m = message(text="https://youtu.be/dQw4w9WgXcQ")
        await bot.on_link(m)
        self.assertIn("Как нарезать", texts(m))
        c = callback("mode:manual")
        await bot.mode_cb(c)
        markup = c.message.answer.call_args.kwargs["reply_markup"]
        url = markup.keyboard[0][0].web_app.url
        self.assertIn("v=dQw4w9WgXcQ", url)
        self.assertIn("max=5", url)

        step = message(text="11:24")
        self.assertTrue(bot._in_manual(step))
        await bot.on_manual_text(step)
        self.assertIn("Начало: <b>11:24</b>", texts(step))
        end = message(text="1150")  # цифрами без двоеточия
        await bot.on_manual_text(end)
        self.assertIn("11:24–11:50", texts(end))

        await bot.frag_cb(callback("frag:more"))
        await bot.on_manual_text(message(text="20:00"))
        await bot.length_cb(callback("len:30"))
        await bot.frag_cb(callback("frag:go"))
        (_, _, _, ranges, _), = self.queued()
        self.assertEqual(ranges, [(684, 710), (1200, 1230)])
        self.assertNotIn(UID, bot.drafts)

    async def test_bad_end_time(self):
        await bot.on_link(message(text="https://youtu.be/dQw4w9WgXcQ"))
        await bot.mode_cb(callback("mode:manual"))
        await bot.on_manual_text(message(text="5:00"))
        bad = message(text="4:00")
        await bot.on_manual_text(bad)
        self.assertIn("позже начала", texts(bad))
        self.assertEqual(bot.drafts[UID]["ranges"], [])

    async def test_webapp_window(self):
        await bot.on_link(message(text="https://youtu.be/dQw4w9WgXcQ"))
        await bot.mode_cb(callback("mode:manual"))
        data = json.dumps({"v": 1, "ranges": [[684, 710], [100, 50], [3000, 3100]]})
        m = message(web_app_data=data)
        await bot.on_webapp(m)
        self.assertIn("Пропустил", texts(m))
        (_, _, _, ranges, _), = self.queued()
        self.assertEqual(ranges, [(684, 710), (3000, 3100)])

    async def test_auto_needs_premium(self):
        await bot.on_link(message(text="https://youtu.be/dQw4w9WgXcQ"))
        c = callback("mode:auto")
        await bot.mode_cb(c)
        self.assertEqual(self.queued(), [])
        self.assertIn("Премиум", texts(c.message))
        self.assertIn("start=ref_1", texts(c.message))

        storage.add_premium(UID, 1)
        await bot.mode_cb(callback("mode:auto"))
        (_, _, _, ranges, charge), = self.queued()
        self.assertIsNone(ranges)
        self.assertEqual(charge, {"auto": 1})

    async def test_free_daily_limit(self):
        text = "https://youtu.be/dQw4w9WgXcQ " + ", ".join(f"{i}:00-{i}:20" for i in range(1, 8))
        m = message(text=text)
        await bot.on_link(m)
        (_, _, _, ranges, _), = self.queued()
        self.assertEqual(len(ranges), 5)
        self.assertIn("осталось 5", texts(m))

        m2 = message(text="https://youtu.be/dQw4w9WgXcQ")
        await bot.on_link(m2)
        c = callback("mode:manual")
        await bot.mode_cb(c)
        self.assertIn("закончились", texts(c.message))

    async def test_failed_job_returns_limit(self):
        await bot.on_link(message(text="https://youtu.be/x 0:10-0:20, 0:30-0:40"))
        m, kind, payload, ranges, charge = self.queued()[0]
        self.assertEqual(storage.used_today(UID)["fragments"], 2)
        m.bot.download = AsyncMock(side_effect=RuntimeError("сеть"))
        await bot.process(m, "file", "file_id", ranges, charge)
        self.assertEqual(storage.used_today(UID)["fragments"], 0)

    async def test_referral_reward(self):
        start = message(uid=2, text="/start ref_1")
        await bot.start(start, SimpleNamespace(args="ref_1"))
        self.assertFalse(storage.is_premium(1))
        b = tg_bot()
        await bot.reward_referrer(b, 2)
        self.assertTrue(storage.is_premium(1))
        b.send_message.assert_called_once()
        await bot.reward_referrer(b, 2)  # второй раз награды нет
        self.assertEqual(b.send_message.call_count, 1)

    async def test_self_referral_ignored(self):
        await bot.start(message(text="/start ref_1"), SimpleNamespace(args="ref_1"))
        await bot.reward_referrer(tg_bot(), 1)
        self.assertFalse(storage.is_premium(1))

    async def test_queue_per_user(self):
        for _ in range(2):
            await bot.on_link(message(text="https://youtu.be/x 0:10-0:20"))
        m = message(text="https://youtu.be/x 0:10-0:20")
        await bot.on_link(m)
        self.assertEqual(len(self.queued()), 2)
        self.assertIn("Дождись", texts(m))


if __name__ == "__main__":
    unittest.main()
