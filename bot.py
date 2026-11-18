"""Telegram-бот поверх ядра shorts/. Запуск: python bot.py"""
import asyncio
import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import time
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
from shorts.pipeline import cut_fragments, make_shorts
from shorts.render import probe
from shorts.timecodes import (MAX_FRAGMENT_SEC, MAX_FRAGMENTS, fit_to_duration, fmt, has_timecodes,
                              parse_ranges)

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
# Видео, по которому человек ещё не выбрал режим: {user_id: {"kind", "payload", "msg", "time"}}
drafts: dict[int, dict] = {}
DRAFT_TTL = 3600  # через час недовыбранное видео забываем

TIMECODES_HELP = (
    "✂️ Пришли тайминги — каждый фрагмент с новой строки или через запятую:\n\n"
    "<code>0:45-1:20\n3:10-3:55\n1:02:30-1:03:15</code>\n\n"
    f"До {MAX_FRAGMENTS} фрагментов, каждый не длиннее {MAX_FRAGMENT_SEC // 60} минут. "
    "Можно и в секундах: <code>80-105</code>.")
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
    s = user_settings.setdefault(uid, {})
    for key, value in (("unique", "light"), ("layout", "blur"),
                       ("clips", config.DEFAULT_CLIPS), ("subs", 1)):
        s.setdefault(key, value)
    return s


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
        [btn("💬 С субтитрами", "subs", 1), btn("Без субтитров", "subs", 0)],
    ])


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Авто: найти моменты", callback_data="mode:auto")],
        [InlineKeyboardButton(text="✂️ Свои тайминги", callback_data="mode:manual")],
    ])


dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message) -> None:
    limit = f"\nБесплатно: {DAILY_LIMIT} видео в день." if DAILY_LIMIT > 0 else ""
    await m.answer(
        "👋 Привет! Пришли мне длинное видео (файлом) или ссылку на YouTube — "
        "я найду интересные моменты и сделаю вертикальные шортсы с субтитрами.\n\n"
        "✂️ Хочешь нарезать сам? Выбери «Свои тайминги» или сразу напиши их вместе со ссылкой:\n"
        "<code>https://youtu.be/... 1:20-1:45, 5:10-5:40</code>\n\n"
        f"⚙️ /settings — уникализация, формат и количество шортсов.\n"
        f"📊 /limit — сколько видео осталось на сегодня.{limit}\n\n"
        "Загружай только видео, на которые у тебя есть права.", parse_mode="HTML")


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
    allowed = {"unique": ("none", "light", "strong"), "layout": ("blur", "crop"),
               "clips": ("3", "5", "8"), "subs": ("0", "1")}
    if value not in allowed.get(key, ()):
        await c.answer()
        return
    s[key] = int(value) if key in ("clips", "subs") else value
    save_state()
    try:
        await c.message.edit_reply_markup(reply_markup=settings_keyboard(s))
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено")


async def enqueue(m: Message, kind: str, payload: str,
                  ranges: list[tuple[float, float]] | None = None) -> None:
    if not check_limit(m.from_user.id):
        await m.answer(f"⏳ Лимит {DAILY_LIMIT} видео в день исчерпан. Возвращайся завтра!")
        return
    uid = m.from_user.id
    if pending.get(uid, 0) >= MAX_QUEUE_PER_USER and uid not in ADMIN_IDS:
        await m.answer("⏳ Дождись, пока обработаются предыдущие видео.")
        return
    pending[uid] = pending.get(uid, 0) + 1
    await queue.put((m, kind, payload, ranges))
    pos = queue.qsize()
    await m.answer("✅ Принял! " + (f"Ты в очереди: {pos}-й." if pos > 1 else "Начинаю обработку..."))


async def receive(m: Message, kind: str, payload: str, text: str | None) -> None:
    """Пришло видео: если тайминги уже написаны — режем их, иначе спрашиваем режим."""
    uid = m.from_user.id
    if has_timecodes(text):
        ranges, errors = parse_ranges(text)
        if errors:
            await m.answer("⚠️ " + "\n⚠️ ".join(errors))
        if ranges:
            await enqueue(m, kind, payload, ranges)
            return
    drafts[uid] = {"kind": kind, "payload": payload, "msg": m, "time": time.time()}
    await m.answer("Как нарезать?", reply_markup=mode_keyboard())


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
    await receive(m, "file", media.file_id, m.caption)


@dp.message(F.text.regexp(YT_RE))
async def on_link(m: Message) -> None:
    await receive(m, "youtube", YT_RE.search(m.text).group(0), m.text)


def _draft(uid: int) -> dict | None:
    d = drafts.get(uid)
    if d and time.time() - d["time"] > DRAFT_TTL:
        drafts.pop(uid, None)
        return None
    return d


@dp.callback_query(F.data.startswith("mode:"))
async def mode_cb(c: CallbackQuery) -> None:
    d = _draft(c.from_user.id)
    if not d:
        await c.answer("Пришли видео ещё раз", show_alert=True)
        return
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if c.data == "mode:auto":
        drafts.pop(c.from_user.id, None)
        await enqueue(d["msg"], d["kind"], d["payload"])
    else:
        d["waiting_times"] = True
        await c.message.answer(TIMECODES_HELP, parse_mode="HTML")


@dp.message(F.text, lambda m: has_timecodes(m.text))
async def on_timecodes(m: Message) -> None:
    d = _draft(m.from_user.id)
    if not d:
        await m.answer("Сначала пришли видео или ссылку на YouTube, потом тайминги 🎬")
        return
    ranges, errors = parse_ranges(m.text)
    if errors:
        await m.answer("⚠️ " + "\n⚠️ ".join(errors))
    if not ranges:
        await m.answer(TIMECODES_HELP, parse_mode="HTML")
        return
    drafts.pop(m.from_user.id, None)
    await enqueue(d["msg"], d["kind"], d["payload"], ranges)


@dp.message()
async def fallback(m: Message) -> None:
    d = _draft(m.from_user.id)
    if d and d.get("waiting_times"):
        await m.answer("Не понял тайминги 🤔\n\n" + TIMECODES_HELP, parse_mode="HTML")
        return
    await m.answer("Пришли видеофайл или ссылку на YouTube 🎬")


YT_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"


def download_youtube_sections(url: str, ranges: list[tuple[float, float]],
                              work: str) -> tuple[list[dict], list[str]]:
    """Скачивает с YouTube только нужные куски, а не всё видео — быстрее и почти не занимает диск."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    ranges, errors = fit_to_duration(ranges, float(info.get("duration") or 0) or float("inf"))
    fragments = []
    for i, (start, end) in enumerate(ranges, 1):
        opts = {
            "format": YT_FORMAT,
            "outtmpl": os.path.join(work, f"part_{i:02d}.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True, "noplaylist": True,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,  # точный рез, а не по ближайшему ключевому кадру
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = sorted(glob.glob(os.path.join(work, f"part_{i:02d}.*")))
        if not files:
            raise ValueError(f"не удалось скачать фрагмент {fmt(start)}-{fmt(end)}")
        fragments.append({"video": files[0], "start": 0, "end": end - start,
                          "orig_start": start, "orig_end": end})
    return fragments, errors


def download_youtube(url: str, work: str) -> str:
    import yt_dlp
    opts = {
        "format": YT_FORMAT,
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


async def process(m: Message, kind: str, payload: str,
                  ranges: list[tuple[float, float]] | None = None) -> None:
    loop = asyncio.get_running_loop()
    status = await m.answer("⏬ Скачиваю видео..." if not (ranges and kind == "youtube")
                            else f"⏬ Скачиваю только нужные фрагменты ({len(ranges)})...")

    def progress(text: str) -> None:  # вызывается из рабочего потока
        asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

    work = tempfile.mkdtemp(prefix="job_")
    s = settings_for(m.from_user.id)
    style = {"unique": s["unique"], "layout": s["layout"], "subtitles": bool(s["subs"]),
             "progress": progress}
    try:
        fragments, errors = None, []
        if ranges and kind == "youtube":
            try:
                fragments, errors = await asyncio.to_thread(download_youtube_sections, payload, ranges, work)
            except Exception:  # noqa: BLE001 — не вышло кусками, качаем целиком
                logging.exception("Не удалось скачать фрагменты, качаю видео целиком")
                await _safe_edit(status, "⏬ Скачиваю видео целиком...")

        if fragments is None:
            if kind == "file":
                src = os.path.join(work, "source.mp4")
                await m.bot.download(payload, destination=src)
            else:
                src = await asyncio.to_thread(download_youtube, payload, work)
            info = await asyncio.to_thread(probe, src)
            if ranges:
                ranges, errors = fit_to_duration(ranges, info["duration"])
                fragments = [{"video": src, "start": a, "end": b} for a, b in ranges]
            elif info["duration"] > MAX_VIDEO_MIN * 60:
                await _safe_edit(status, f"😔 Видео длиннее {MAX_VIDEO_MIN} минут.")
                return

        if errors:
            await m.answer("⚠️ " + "\n⚠️ ".join(errors))
        if fragments is not None and not fragments:
            await _safe_edit(status, "😔 Ни один тайминг не попал внутрь видео.")
            return

        out = os.path.join(work, "out")
        if fragments is not None:
            results = await asyncio.to_thread(cut_fragments, fragments, out, **style)
        else:
            results = await asyncio.to_thread(make_shorts, src, out, n_clips=s["clips"], **style)

        await _safe_edit(status, f"📤 Готово! Отправляю {len(results)} шортс(ов)...")
        for r in results:
            icon = "✂️" if fragments is not None else "🎬"
            caption = f"{icon} {r['title']}\n⏱ {fmt(r['start'])}–{fmt(r['end'])}"
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


async def worker() -> None:
    """Обрабатываем по одному видео за раз — слабому серверу так легче."""
    while True:
        m, kind, payload, ranges = await queue.get()
        try:
            await process(m, kind, payload, ranges)
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
