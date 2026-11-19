"""Тесты логики бота без Telegram (нужен aiogram): python -m unittest discover tests"""
import asyncio
import json
import logging
import os
import tempfile
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
    b.get_file = AsyncMock(return_value=SimpleNamespace(file_path="documents/file_1.mp4"))
    b.download_file = AsyncMock()
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
        bot.config.GROQ_API_KEY = ""
        bot.config.LLM_API_KEY = ""
        storage._db.execute("DELETE FROM users")
        storage._db.execute("DELETE FROM usage")
        storage._db.execute("DELETE FROM events")
        storage._db.execute("DELETE FROM errors")
        bot.FOUNDERS_PREMIUM_DAYS = 0
        bot.local_files.clear()

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
        stub = callback("mode:auto")
        await bot.mode_cb(stub)  # ИИ не подключён — заглушка
        self.assertIn("скоро", texts(stub.message))
        self.assertEqual(self.queued(), [])

        bot.config.GROQ_API_KEY = "test"
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
        m.bot.download_file = AsyncMock(side_effect=RuntimeError("сеть"))
        await bot.process(m, "file", "file_id", ranges, charge)
        self.assertEqual(storage.used_today(UID)["fragments"], 0)

    async def test_referral_reward(self):
        storage.register(1)
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

    async def test_buy_stub_counts_interest(self):
        c = callback("buy:30")
        await bot.buy_cb(c)
        self.assertIn("скоро", texts(c.message))
        self.assertEqual(storage.event_count("buy_click"), 1)
        menu = callback("buy:menu")
        await bot.buy_cb(menu)
        kb = menu.message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual(len(kb.inline_keyboard), len(bot.payments.PLANS))

    async def test_stats_shows_services(self):
        bot.ADMIN_IDS = {UID}
        m = message()
        await bot.stats_cmd(m)
        self.assertIn("заглушка", texts(m))

    async def test_founders_get_premium(self):
        bot.FOUNDERS_LIMIT, bot.FOUNDERS_PREMIUM_DAYS = 2, 7
        msgs = []
        for uid in (10, 11, 12):
            m = message(uid=uid, text="/start")
            await bot.start(m, SimpleNamespace(args=None))
            msgs.append(texts(m))
        self.assertIn("№1</b> из первых 2", msgs[0])
        self.assertIn("№2</b>", msgs[1])
        self.assertNotIn("первых", msgs[2])
        self.assertTrue(storage.is_premium(10) and storage.is_premium(11))
        self.assertFalse(storage.is_premium(12))
        again = message(uid=10, text="/start")  # повторный /start ничего не дарит
        await bot.start(again, SimpleNamespace(args=None))
        self.assertNotIn("первых", texts(again))

    async def test_invited_friend_gets_bonus(self):
        storage.register(1)
        m = message(uid=2, text="/start ref_1")
        await bot.start(m, SimpleNamespace(args="ref_1"))
        self.assertIn("+5 фрагментов", texts(m))
        self.assertEqual(bot.daily_limit(2), 10)
        fake = message(uid=3, text="/start ref_999")  # несуществующий пригласивший
        await bot.start(fake, SimpleNamespace(args="ref_999"))
        self.assertEqual(bot.daily_limit(3), 5)
        self.assertIsNone(storage.get_user(3)["referrer"])

    async def test_watermark_bonus_and_settings(self):
        c = callback("set:wm:1")
        await bot.settings_cb(c)
        self.assertEqual(bot.daily_limit(UID), 8)
        await bot.settings_cb(callback("set:style:neon"))
        await bot.settings_cb(callback("set:style:hacker"))  # чужое значение игнорируем
        self.assertEqual(bot.settings_for(UID)["style"], "neon")
        m = message(text="/settings")
        await bot.settings_cmd(m)
        kb = m.answer.call_args.kwargs["reply_markup"]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("✅ Неон", labels)

    async def test_process_uses_look_cache_and_share(self):
        await bot.settings_cb(callback("set:wm:1"))
        await bot.on_link(message(text="https://youtu.be/x"))  # регистрация
        src = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        src.write(b"video")
        src.close()
        bot.local_files["file1"] = (src.name, 0)  # уже скачано для раскадровки
        captured = {}

        def fake_cut(fragments, out, **kw):
            captured.update(kw, fragments=fragments)
            return [{"path": "x.mp4", "width": 720, "height": 1280, "title": "Фрагмент 1",
                     "start": 10, "end": 20}]
        m = message()
        orig = (bot.probe, bot.cut_fragments)
        bot.probe = lambda path: {"duration": 100, "has_audio": True}
        bot.cut_fragments = fake_cut
        try:
            await bot.process(m, "file", "file1", [(10, 20)], {"fragments": 1})
        finally:
            bot.probe, bot.cut_fragments = orig
        m.bot.download_file.assert_not_called()  # второй раз не скачивали
        self.assertEqual(captured["watermark"], "@test_bot")
        self.assertTrue(captured["hook"])
        status = m.answer.return_value
        markup = status.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("t.me/share/url", markup.inline_keyboard[0][0].url)
        self.assertIn("start%3Dref_1", markup.inline_keyboard[0][0].url)

    async def test_reminders(self):
        storage.register(1)
        storage.touch(1)
        storage.set_remind(1, True)
        storage.register(2)
        storage.touch(2)
        storage.set_remind(2, True)
        b = tg_bot()
        b.send_message = AsyncMock(side_effect=[None, bot.TelegramForbiddenError(method=MagicMock(),
                                                                                  message="blocked")])
        self.assertEqual(await bot.send_reminders(b), 1)
        self.assertEqual(storage.get_user(2)["blocked"], 1)
        self.assertEqual(await bot.send_reminders(b), 0)  # второй раз за день не шлём

    async def test_reminder_button(self):
        await bot.remind_cb(callback("remind:1"))
        self.assertEqual(storage.get_user(UID)["remind"], 1)
        await bot.remind_cb(callback("remind:0"))
        self.assertEqual(storage.get_user(UID)["remind"], 0)

    async def test_broadcast(self):
        bot.ADMIN_IDS = {UID}
        for uid in (1, 2, 3):
            storage.register(uid)
        m = message(text="/broadcast")
        m.reply_to_message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=77)
        await bot.broadcast_cmd(m)
        self.assertIn("3 пользователям", texts(m))
        b = tg_bot()
        b.copy_message = AsyncMock(side_effect=[None, bot.TelegramForbiddenError(method=MagicMock(),
                                                                                  message="x"), None])
        await bot.run_broadcast(b, UID, 1, 77)
        self.assertEqual(b.copy_message.call_count, 3)
        report = b.send_message.call_args.args[1]
        self.assertIn("Доставлено: 2", report)
        self.assertEqual(storage.get_user(2)["blocked"], 1)
        not_admin = message(uid=5, text="/broadcast")
        not_admin.reply_to_message = m.reply_to_message
        await bot.broadcast_cmd(not_admin)
        not_admin.answer.assert_not_called()

    async def test_error_log(self):
        bot.ADMIN_IDS = {UID}
        logging.getLogger("test").error("что-то сломалось")
        m = message(text="/errors")
        await bot.errors_cmd(m)
        self.assertIn("что-то сломалось", texts(m))
        stats = message(text="/stats")
        await bot.stats_cmd(stats)
        self.assertIn("Ошибок за сутки: 1", texts(stats))

    async def test_queue_per_user(self):
        for _ in range(2):
            await bot.on_link(message(text="https://youtu.be/x 0:10-0:20"))
        m = message(text="https://youtu.be/x 0:10-0:20")
        await bot.on_link(m)
        self.assertEqual(len(self.queued()), 2)
        self.assertIn("Дождись", texts(m))


    @unittest.skipIf(os.name == "nt", "в Windows в имени папки нельзя двоеточие, как у Bot API сервера")
    async def test_bot_api_file_removed_after_download(self):
        with tempfile.TemporaryDirectory() as root:
            token_dir = os.path.join(root, "123:ABC", "videos")
            os.makedirs(token_dir)
            server_file = os.path.join(token_dir, "file_1.mp4")
            open(server_file, "w").close()
            outside = os.path.join(tempfile.gettempdir(), "not_bot_api_file.txt")
            open(outside, "w").close()
            b = tg_bot()
            b.get_file = AsyncMock(return_value=SimpleNamespace(file_path=server_file))
            orig = (bot.BOT_API_URL, bot.BOT_API_DIR)
            bot.BOT_API_URL, bot.BOT_API_DIR = "http://127.0.0.1:8081", root
            try:
                await bot.download_tg(b, "id", os.path.join(root, "copy.mp4"))
                bot.remove_bot_api_file(root, outside)  # чужой файл вне папки сервера не трогаем
            finally:
                bot.BOT_API_URL, bot.BOT_API_DIR = orig
            self.assertFalse(os.path.exists(server_file))
            self.assertTrue(os.path.exists(outside))
            os.remove(outside)

    @unittest.skipIf(os.name == "nt", "в Windows в имени папки нельзя двоеточие, как у Bot API сервера")
    def test_bot_api_cleanup_only_old_files(self):
        with tempfile.TemporaryDirectory() as root:
            token_dir = os.path.join(root, "123:ABC", "videos")
            os.makedirs(token_dir)
            old, fresh = os.path.join(token_dir, "old.mp4"), os.path.join(token_dir, "fresh.mp4")
            other = os.path.join(root, "other.txt")  # не в папке токена — не трогаем
            for path in (old, fresh, other):
                open(path, "w").close()
            past = os.path.getmtime(old) - 2 * 3600
            os.utime(old, (past, past))
            os.utime(other, (past, past))
            self.assertEqual(bot.cleanup_bot_api_dir(root, 60), 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(fresh) and os.path.exists(other))

if __name__ == "__main__":
    unittest.main()
