"""Telegram-бот поверх ядра shorts/. Запуск: python bot.py"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from shorts import config
from shorts.pipeline import make_shorts
from shorts.render import probe

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_API_URL = os.getenv("BOT_API_URL", "")  # свой Local Bot API Server (для файлов до 2 ГБ)
# Если Bot API сервер в Docker: папка файлов внутри контейнера и та же папка на компьютере
BOT_API_SERVER_DIR = os.getenv("BOT_API_SERVER_DIR", "")
BOT_API_LOCAL_DIR = os.getenv("BOT_API_LOCAL_DIR", "")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "3"))  # 0 — без лимита
MAX_VIDEO_MIN = int(os.getenv("MAX_VIDEO_MIN", "90"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

YT_RE = re.compile(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+")

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))  # настройки и лимиты переживают перезапуск
MAX_QUEUE_PER_USER = 2  # сколько видео один человек может держать в очереди

user_settings: dict[int, dict] = {}  # {user_id: {"unique": ..., "layout": ..., "clips": ...}}
usage: dict[int, tuple[dt.date, int]] = {}  # {user_id: (дата, сколько видео сегодня)}
pending: dict[int, int] = {}  # {user_id: сколько видео ждёт в очереди}
queue: asyncio.Queue = asyncio.Queue()


def load_state() -> None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for uid, s in data.get("settings", {}).items():
        user_settings[int(uid)] = s
    for uid, (day, count) in data.get("usage", {}).items():
        usage[int(uid)] = (dt.date.fromisoformat(day), count)


def save_state() -> None:
    data = {"settings": user_settings,
            "usage": {uid: [day.isoformat(), count] for uid, (day, count) in usage.items()}}
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        logging.exception("Не удалось сохранить state.json")


def settings_for(uid: int) -> dict:
    return user_settings.setdefault(uid, {"unique": "light", "layout": "blur",
                                          "clips": config.DEFAULT_CLIPS})


def used_today(uid: int) -> int:
    day, count = usage.get(uid, (dt.date.today(), 0))
    return count if day == dt.date.today() else 0


def check_limit(uid: int) -> bool:
    if DAILY_LIMIT <= 0 or uid in ADMIN_IDS:
        return True
    # считаем и уже обработанные, и те, что ещё стоят в очереди
    return used_today(uid) + pending.get(uid, 0) < DAILY_LIMIT


def add_usage(uid: int) -> None:
    usage[uid] = (dt.date.today(), used_today(uid) + 1)
    save_state()


def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    def btn(text: str, key: str, value) -> InlineKeyboardButton:
        mark = "✅ " if s[key] == value else ""
        return InlineKeyboardButton(text=mark + text, callback_data=f"set:{key}:{value}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Без уникализации", "unique", "none"), btn("Лёгкая", "unique", "light"),
         btn("Сильная", "unique", "strong")],
        [btn("Размытый фон", "layout", "blur"), btn("Обрезка", "layout", "crop")],
        [btn("3 шортса", "clips", 3), btn("5 шортсов", "clips", 5), btn("8 шортсов", "clips", 8)],
    ])


dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message) -> None:
    limit = f"\nБесплатно: {DAILY_LIMIT} видео в день." if DAILY_LIMIT > 0 else ""
    await m.answer(
        "👋 Привет! Пришли мне длинное видео (файлом) или ссылку на YouTube — "
        "я найду интересные моменты и сделаю вертикальные шортсы с субтитрами.\n\n"
        f"⚙️ /settings — уникализация, формат и количество шортсов.\n"
        f"📊 /limit — сколько видео осталось на сегодня.{limit}\n\n"
        "Загружай только видео, на которые у тебя есть права.")


@dp.message(Command("limit"))
async def limit_cmd(m: Message) -> None:
    uid = m.from_user.id
    if DAILY_LIMIT <= 0 or uid in ADMIN_IDS:
        await m.answer("У тебя нет лимита ♾")
    else:
        await m.answer(f"Сегодня использовано {used_today(uid)} из {DAILY_LIMIT} видео.")


@dp.message(Command("settings"))
async def settings_cmd(m: Message) -> None:
    await m.answer("Настройки нарезки:", reply_markup=settings_keyboard(settings_for(m.from_user.id)))


@dp.callback_query(F.data.startswith("set:"))
async def settings_cb(c: CallbackQuery) -> None:
    _, key, value = c.data.split(":", 2)
    s = settings_for(c.from_user.id)
    allowed = {"unique": ("none", "light", "strong"), "layout": ("blur", "crop"), "clips": ("3", "5", "8")}
    if value not in allowed.get(key, ()):
        await c.answer()
        return
    s[key] = int(value) if key == "clips" else value
    save_state()
    try:
        await c.message.edit_reply_markup(reply_markup=settings_keyboard(s))
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено")


async def enqueue(m: Message, kind: str, payload: str) -> None:
    if not check_limit(m.from_user.id):
        await m.answer(f"⏳ Лимит {DAILY_LIMIT} видео в день исчерпан. Возвращайся завтра!")
        return
    uid = m.from_user.id
    if pending.get(uid, 0) >= MAX_QUEUE_PER_USER and uid not in ADMIN_IDS:
        await m.answer("⏳ Дождись, пока обработаются предыдущие видео.")
        return
    pending[uid] = pending.get(uid, 0) + 1
    await queue.put((m, kind, payload))
    pos = queue.qsize()
    await m.answer("✅ Принял! " + (f"Ты в очереди: {pos}-й." if pos > 1 else "Начинаю обработку..."))


@dp.message(F.video | F.document)
async def on_video(m: Message) -> None:
    media = m.video or m.document
    if m.document and not (m.document.mime_type or "").startswith("video/"):
        await m.answer("Это не видео 🤔 Пришли видеофайл.")
        return
    if not BOT_API_URL and (media.file_size or 0) > 20 * 1024 * 1024:
        await m.answer("😔 Файл больше 20 МБ — обычный Telegram API такие боту не отдаёт.\n"
                       "Пришли ссылку на YouTube или видео поменьше.")
        return
    await enqueue(m, "file", media.file_id)


@dp.message(F.text.regexp(YT_RE))
async def on_link(m: Message) -> None:
    await enqueue(m, "youtube", YT_RE.search(m.text).group(0))


@dp.message()
async def fallback(m: Message) -> None:
    await m.answer("Пришли видеофайл или ссылку на YouTube 🎬")


def download_youtube(url: str, work: str) -> str:
    import yt_dlp
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "outtmpl": os.path.join(work, "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True, "noplaylist": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_VIDEO_MIN * 60}"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise ValueError("не удалось скачать видео")
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        path = os.path.splitext(path)[0] + ".mp4"
    if not os.path.exists(path):
        raise ValueError(f"видео длиннее {MAX_VIDEO_MIN} минут или недоступно")
    return path


async def process(m: Message, kind: str, payload: str) -> None:
    loop = asyncio.get_running_loop()
    status = await m.answer("⏬ Скачиваю видео...")

    def progress(text: str) -> None:  # вызывается из рабочего потока
        asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

    work = tempfile.mkdtemp(prefix="job_")
    try:
        if kind == "file":
            src = os.path.join(work, "source.mp4")
            await m.bot.download(payload, destination=src)
        else:
            src = await asyncio.to_thread(download_youtube, payload, work)

        info = await asyncio.to_thread(probe, src)
        if info["duration"] > MAX_VIDEO_MIN * 60:
            await _safe_edit(status, f"😔 Видео длиннее {MAX_VIDEO_MIN} минут.")
            return

        s = settings_for(m.from_user.id)
        results = await asyncio.to_thread(
            make_shorts, src, os.path.join(work, "out"), n_clips=s["clips"],
            unique=s["unique"], layout=s["layout"], progress=progress)

        await _safe_edit(status, f"📤 Готово! Отправляю {len(results)} шортс(ов)...")
        for r in results:
            caption = f"🎬 {r['title']}\n⏱ {_mmss(r['start'])}–{_mmss(r['end'])}"
            await m.answer_video(FSInputFile(r["path"]), caption=caption, supports_streaming=True,
                                 width=r["width"], height=r["height"],
                                 duration=round(r["end"] - r["start"]))
        add_usage(m.from_user.id)
        await _safe_edit(status, "✅ Готово! Присылай следующее видео.")
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка обработки")
        await _safe_edit(status, f"❌ Не получилось: {str(e)[:300]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)  # диск маленький — сразу всё удаляем


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


async def worker() -> None:
    """Обрабатываем по одному видео за раз — слабому серверу так легче."""
    while True:
        m, kind, payload = await queue.get()
        try:
            await process(m, kind, payload)
        except Exception:  # noqa: BLE001 — очередь не должна останавливаться из-за одной ошибки
            logging.exception("Сбой в обработчике очереди")
        finally:
            uid = m.from_user.id
            pending[uid] = max(0, pending.get(uid, 0) - 1)
            queue.task_done()


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в файле .env")
    if not shutil.which(config.FFMPEG):
        raise SystemExit("FFmpeg не найден. Установи его (см. README) и запусти python check.py")
    load_state()
    session = None
    if BOT_API_URL:
        extra = {}
        if BOT_API_SERVER_DIR and BOT_API_LOCAL_DIR:
            extra["wrap_local_file"] = SimpleFilesPathWrapper(Path(BOT_API_SERVER_DIR),
                                                              Path(BOT_API_LOCAL_DIR))
        session = AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_URL, is_local=True, **extra))
    bot = Bot(BOT_TOKEN, session=session)
    asyncio.create_task(worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
