"""Telegram-бот поверх ядра shorts/. Запуск: python bot.py

Режимы:
  ✂️ Свои тайминги — бесплатно, с дневным лимитом фрагментов
  🤖 Авто-нарезка  — только для премиума
Премиум можно получить за приглашённых друзей (временный) или выдать командой /give.
"""
import asyncio
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()

import storage  # noqa: E402 — путь к базе берётся из .env
from shorts import config  # noqa: E402
from shorts.pipeline import cut_fragments, make_shorts  # noqa: E402
from shorts.render import probe  # noqa: E402
from shorts.timecodes import (MAX_FRAGMENT_SEC, MAX_FRAGMENTS, check_range,  # noqa: E402
                              fit_to_duration, fmt, has_timecodes, parse_ranges, parse_single)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_API_URL = os.getenv("BOT_API_URL", "")  # свой Local Bot API Server (для файлов до 2 ГБ)
# Если Bot API сервер в Docker: папка файлов внутри контейнера и та же папка на компьютере
BOT_API_SERVER_DIR = os.getenv("BOT_API_SERVER_DIR", "")
BOT_API_LOCAL_DIR = os.getenv("BOT_API_LOCAL_DIR", "")
# Адрес окна разметки таймингов (docs/timecodes.html на GitHub Pages). Пусто — только ввод в чате
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

FREE_FRAGMENTS_PER_DAY = _env_int("FREE_FRAGMENTS_PER_DAY", 5)      # бесплатно: фрагментов в день
PREMIUM_FRAGMENTS_PER_DAY = _env_int("PREMIUM_FRAGMENTS_PER_DAY", 0)  # премиум: 0 — без лимита
PREMIUM_AUTO_PER_DAY = _env_int("PREMIUM_AUTO_PER_DAY", 5)          # премиум: авто-нарезок в день
REF_PREMIUM_DAYS = _env_int("REF_PREMIUM_DAYS", 3)  # дней премиума за приглашённого активного друга
PREMIUM_BUY_TEXT = os.getenv("PREMIUM_BUY_TEXT", "💳 Оплата подписки появится совсем скоро.")
MAX_VIDEO_MIN = _env_int("MAX_VIDEO_MIN", 90)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

YT_RE = re.compile(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+")
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})")
MAX_QUEUE_PER_USER = 2  # сколько задач один человек может держать в очереди
DRAFT_TTL = 3600        # через час недоделанную разметку забываем

pending: dict[int, int] = {}  # {user_id: сколько задач ждёт в очереди}
# Видео, по которому человек ещё выбирает режим или размечает тайминги
drafts: dict[int, dict] = {}
queue: asyncio.Queue = asyncio.Queue()

dp = Dispatcher()


# ---------------------------------------------------------------- лимиты ----

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def has_premium(uid: int) -> bool:
    return is_admin(uid) or storage.is_premium(uid)


def fragments_left(uid: int) -> int:
    """Сколько фрагментов ещё можно нарезать сегодня (не больше MAX_FRAGMENTS за раз)."""
    if is_admin(uid):
        return MAX_FRAGMENTS
    limit = PREMIUM_FRAGMENTS_PER_DAY if storage.is_premium(uid) else FREE_FRAGMENTS_PER_DAY
    if limit <= 0:
        return MAX_FRAGMENTS
    return max(0, min(MAX_FRAGMENTS, limit - storage.used_today(uid)["fragments"]))


def auto_left(uid: int) -> int:
    if is_admin(uid) or PREMIUM_AUTO_PER_DAY <= 0:
        return 99
    if not storage.is_premium(uid):
        return 0
    return max(0, PREMIUM_AUTO_PER_DAY - storage.used_today(uid)["auto"])


async def ref_link(bot: Bot, uid: int) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=ref_{uid}"


async def premium_pitch(m: Message, uid: int) -> None:
    link = await ref_link(m.bot, uid)
    await m.answer(
        "👑 <b>Авто-нарезка доступна в Премиуме</b>\n\n"
        "Бот сам найдёт самые цепляющие моменты в видео и смонтирует шортсы.\n\n"
        f"🎁 <b>Бесплатно:</b> пригласи друга — когда он сделает первую нарезку, "
        f"ты получишь {REF_PREMIUM_DAYS} дн. Премиума. Друзей можно звать сколько угодно.\n"
        f"Твоя ссылка: {link}\n\n"
        f"{PREMIUM_BUY_TEXT}\n\n"
        f"А пока — ✂️ «Свои тайминги» бесплатно: {FREE_FRAGMENTS_PER_DAY} фрагментов каждый день.",
        parse_mode="HTML", disable_web_page_preview=True)


async def limit_reached(m: Message, uid: int) -> None:
    link = await ref_link(m.bot, uid)
    await m.answer(
        "⏳ Бесплатные фрагменты на сегодня закончились — завтра будут новые!\n\n"
        f"🎁 Не хочешь ждать? Пригласи друга и получи {REF_PREMIUM_DAYS} дн. Премиума "
        f"с безлимитной нарезкой:\n{link}", disable_web_page_preview=True)


# ------------------------------------------------------------- настройки ----

def settings_for(uid: int) -> dict:
    s = storage.get_settings(uid)
    for key, value in (("unique", "light"), ("layout", "blur"),
                       ("clips", config.DEFAULT_CLIPS), ("subs", 1)):
        s.setdefault(key, value)
    return s


def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    def btn(text: str, key: str, value) -> InlineKeyboardButton:
        mark = "✅ " if s[key] == value else ""
        return InlineKeyboardButton(text=mark + text, callback_data=f"set:{key}:{value}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Без уникализации", "unique", "none"), btn("Лёгкая", "unique", "light"),
         btn("Сильная", "unique", "strong")],
        [btn("Размытый фон", "layout", "blur"), btn("Обрезка", "layout", "crop")],
        [btn("💬 С субтитрами", "subs", 1), btn("Без субтитров", "subs", 0)],
        [btn("Авто: 3 шортса", "clips", 3), btn("5", "clips", 5), btn("8", "clips", 8)],
    ])


# --------------------------------------------------------------- команды ----

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject | None = None) -> None:
    uid = m.from_user.id
    args = (command.args or "") if command else ""
    referrer = int(args[4:]) if args.startswith("ref_") and args[4:].isdigit() else None
    storage.register(uid, referrer)
    await m.answer(
        "👋 Привет! Я режу длинные видео на вертикальные шортсы с субтитрами.\n\n"
        "1️⃣ Пришли видео файлом или ссылку на YouTube\n"
        "2️⃣ Отметь, какие куски вырезать — в удобном окне или прямо в чате\n"
        "3️⃣ Получи готовые шортсы 🎬\n\n"
        f"✂️ Бесплатно: {FREE_FRAGMENTS_PER_DAY} фрагментов каждый день\n"
        "👑 Премиум: авто-нарезка — бот сам найдёт лучшие моменты\n\n"
        "/limit — остаток на сегодня · /ref — пригласить друга · /settings — настройки\n\n"
        "Загружай только видео, на которые у тебя есть права.")


@dp.message(Command("limit"))
async def limit_cmd(m: Message) -> None:
    uid = m.from_user.id
    until = storage.premium_until(uid)
    lines = []
    if is_admin(uid):
        lines.append("🛠 Ты админ — без лимитов ♾")
    elif until:
        lines.append(f"👑 Премиум до {until:%d.%m.%Y %H:%M} (UTC)")
        lines.append(f"🤖 Авто-нарезок сегодня осталось: {auto_left(uid)}")
        lines.append("✂️ Фрагменты: " + ("без лимита ♾" if PREMIUM_FRAGMENTS_PER_DAY <= 0
                                        else f"осталось {fragments_left(uid)}"))
    else:
        lines.append(f"✂️ Бесплатных фрагментов сегодня осталось: {fragments_left(uid)} "
                     f"из {FREE_FRAGMENTS_PER_DAY}")
        lines.append("🔄 Лимит обновляется каждый день.\n👑 Премиум — /premium")
    await m.answer("\n".join(lines))


@dp.message(Command("premium"))
async def premium_cmd(m: Message) -> None:
    until = storage.premium_until(m.from_user.id)
    if until:
        await m.answer(f"👑 Премиум активен до {until:%d.%m.%Y %H:%M} (UTC).\n"
                       f"Продлить можно, пригласив друзей: /ref")
    else:
        await premium_pitch(m, m.from_user.id)


@dp.message(Command("ref"))
async def ref_cmd(m: Message) -> None:
    uid = m.from_user.id
    st = storage.referral_stats(uid)
    await m.answer(
        "🎁 <b>Приглашай друзей — получай Премиум</b>\n\n"
        f"За каждого друга, который сделает хотя бы одну нарезку, — {REF_PREMIUM_DAYS} дн. Премиума. "
        "Дни складываются.\n\n"
        f"Твоя ссылка:\n{await ref_link(m.bot, uid)}\n\n"
        f"👥 Пришло по ссылке: {st['invited']}\n✅ Активных: {st['active']}",
        parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("give"))
async def give_cmd(m: Message, command: CommandObject) -> None:
    """Админ: /give <id> <дней> — выдать премиум."""
    if not is_admin(m.from_user.id):
        return
    try:
        uid, days = (command.args or "").split()
        until = storage.add_premium(int(uid), float(days))
    except ValueError:
        await m.answer("Формат: /give <id пользователя> <дней>")
        return
    await m.answer(f"✅ Премиум для {uid} до {until:%d.%m.%Y %H:%M} (UTC)")
    try:
        await m.bot.send_message(int(uid), f"👑 Тебе выдан Премиум до {until:%d.%m.%Y}!")
    except Exception:  # noqa: BLE001 — человек мог заблокировать бота
        pass


@dp.message(Command("stats"))
async def stats_cmd(m: Message) -> None:
    if not is_admin(m.from_user.id):
        return
    s = storage.stats()
    await m.answer(f"👥 Пользователей: {s['users']}\n👑 С премиумом: {s['premium']}\n"
                   f"📅 Активны сегодня: {s['active_today']}\n"
                   f"✂️ Фрагментов сегодня: {s['fragments_today']}\n🤖 Авто сегодня: {s['auto_today']}\n"
                   f"⏳ В очереди: {queue.qsize()}")


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
    storage.save_settings(c.from_user.id, s)
    try:
        await c.message.edit_reply_markup(reply_markup=settings_keyboard(s))
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено")


@dp.message(Command("cancel"))
async def cancel_cmd(m: Message) -> None:
    drafts.pop(m.from_user.id, None)
    await m.answer("Отменил. Присылай новое видео 🎬", reply_markup=ReplyKeyboardRemove())


# ------------------------------------------------------- приём видео -------

def _draft(uid: int) -> dict | None:
    d = drafts.get(uid)
    if d and time.time() - d["time"] > DRAFT_TTL:
        drafts.pop(uid, None)
        return None
    return d


def mode_keyboard(uid: int) -> InlineKeyboardMarkup:
    auto = "🤖 Авто-нарезка" + ("" if has_premium(uid) else " 👑")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✂️ Свои тайминги", callback_data="mode:manual")],
        [InlineKeyboardButton(text=auto, callback_data="mode:auto")],
    ])


async def receive(m: Message, kind: str, payload: str, text: str | None, duration: float = 0) -> None:
    """Пришло видео: тайминги уже написаны — режем их, иначе спрашиваем режим."""
    uid = m.from_user.id
    storage.register(uid)
    yt = YT_ID_RE.search(payload) if kind == "youtube" else None
    drafts[uid] = {"kind": kind, "payload": payload, "msg": m, "time": time.time(),
                   "yt_id": yt.group(1) if yt else "", "duration": duration,
                   "ranges": [], "step": None}
    if has_timecodes(text):
        ranges, errors = parse_ranges(text, duration or None)
        if errors:
            await m.answer("⚠️ " + "\n⚠️ ".join(errors))
        if ranges:
            await submit_manual(m, uid, ranges)
            return
    await m.answer("Как нарезать?", reply_markup=mode_keyboard(uid))


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
    duration = float(m.video.duration or 0) if m.video else 0
    await receive(m, "file", media.file_id, m.caption, duration)


@dp.message(F.text.regexp(YT_RE))
async def on_link(m: Message) -> None:
    await receive(m, "youtube", YT_RE.search(m.text).group(0), m.text)


@dp.callback_query(F.data.startswith("mode:"))
async def mode_cb(c: CallbackQuery) -> None:
    uid = c.from_user.id
    d = _draft(uid)
    if not d:
        await c.answer("Пришли видео ещё раз", show_alert=True)
        return
    await c.answer()
    if c.data == "mode:auto":
        if not has_premium(uid):
            await premium_pitch(c.message, uid)
            return
        if auto_left(uid) <= 0:
            await c.message.answer("🤖 Авто-нарезки на сегодня закончились. "
                                   "Можно нарезать по своим таймингам ✂️")
            return
        await _hide_buttons(c.message)
        drafts.pop(uid, None)
        await enqueue(d["msg"], d["kind"], d["payload"], None)
        return
    await _hide_buttons(c.message)
    await start_manual(c.message, uid, d)


async def _hide_buttons(msg: Message) -> None:
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


# ---------------------------------------------- ручная разметка таймингов ----

def webapp_url(uid: int, d: dict) -> str:
    params = {"max": max(1, fragments_left(uid) - len(d["ranges"])), "maxlen": MAX_FRAGMENT_SEC}
    if d.get("yt_id"):
        params["v"] = d["yt_id"]
    if d.get("duration"):
        params["dur"] = int(d["duration"])
    return f"{WEBAPP_URL}{'&' if '?' in WEBAPP_URL else '?'}{urlencode(params)}"


def manual_keyboard(uid: int, d: dict) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Кнопка окна разметки над клавиатурой (окно может отправить данные только так)."""
    if not WEBAPP_URL:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🕐 Открыть окно разметки", web_app=WebAppInfo(url=webapp_url(uid, d)))],
        [KeyboardButton(text="/cancel")],
    ])


async def start_manual(m: Message, uid: int, d: dict) -> None:
    left = fragments_left(uid)
    if left <= 0:
        drafts.pop(uid, None)
        await limit_reached(m, uid)
        return
    d.update(step="start", time=time.time())
    window = ("🕐 Нажми «Открыть окно разметки» внизу — там удобные поля"
              + (" и плеер: включаешь видео и ставишь метки кнопками" if d.get("yt_id") else "")
              + ".\n\nИли пиши прямо в чат.\n\n") if WEBAPP_URL else ""
    await m.answer(f"✂️ Можно нарезать до {left} {_plural(left)}.\n\n{window}"
                   "<b>Фрагмент 1.</b> Пришли время начала, например <code>11:24</code>",
                   parse_mode="HTML", reply_markup=manual_keyboard(uid, d))


def length_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"+{n} с", callback_data=f"len:{n}") for n in (15, 30, 45, 60)
    ]])


def summary(uid: int, d: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"{i}. {fmt(a)}–{fmt(b)} ({fmt(b - a)})" for i, (a, b) in enumerate(d["ranges"], 1)]
    left = fragments_left(uid) - len(d["ranges"])
    rows = []
    if left > 0:
        rows.append([InlineKeyboardButton(text="➕ Ещё фрагмент", callback_data="frag:more")])
    rows.append([InlineKeyboardButton(text=f"✂️ Нарезать ({len(d['ranges'])})", callback_data="frag:go")])
    rows.append([InlineKeyboardButton(text="↩️ Убрать последний", callback_data="frag:undo"),
                 InlineKeyboardButton(text="✖️ Отмена", callback_data="frag:cancel")])
    tail = "" if left > 0 else "\n\nЭто максимум на сегодня."
    return "📋 Фрагменты:\n" + "\n".join(lines) + tail, InlineKeyboardMarkup(inline_keyboard=rows)


async def add_ranges(m: Message, uid: int, d: dict, ranges: list[tuple[float, float]]) -> None:
    left = fragments_left(uid) - len(d["ranges"])
    if len(ranges) > left:
        await m.answer(f"⚠️ Осталось только {left} {_plural(left)} — взял первые.")
        ranges = ranges[:max(0, left)]
    d["ranges"].extend(ranges)
    d.update(step=None, time=time.time())
    if d["ranges"]:
        text, kb = summary(uid, d)
        await m.answer(text, reply_markup=kb)


async def submit_manual(m: Message, uid: int, ranges: list[tuple[float, float]]) -> None:
    """Тайминги готовы — проверяем лимит и ставим в очередь."""
    d = drafts.get(uid)
    left = fragments_left(uid)
    if left <= 0:
        drafts.pop(uid, None)
        await limit_reached(m, uid)
        return
    if len(ranges) > left:
        await m.answer(f"⚠️ Сегодня осталось {left} {_plural(left)} — нарежу первые {left}.")
        ranges = ranges[:left]
    drafts.pop(uid, None)
    if d:
        await enqueue(d["msg"], d["kind"], d["payload"], ranges)


@dp.message(F.web_app_data)
async def on_webapp(m: Message) -> None:
    uid = m.from_user.id
    d = _draft(uid)
    if not d:
        await m.answer("Пришли видео ещё раз 🎬", reply_markup=ReplyKeyboardRemove())
        return
    try:
        raw = json.loads(m.web_app_data.data)["ranges"]
        ranges = [(float(a), float(b)) for a, b in raw][:MAX_FRAGMENTS]
    except (ValueError, KeyError, TypeError):
        await m.answer("Не смог прочитать тайминги 😔 Попробуй ещё раз.")
        return
    good, bad = [], []
    for a, b in ranges:
        err = "время не может быть отрицательным" if a < 0 else check_range(a, b)
        (bad if err else good).append(f"{fmt(a)}–{fmt(b)}: {err}" if err else (a, b))
    if bad:
        await m.answer("⚠️ Пропустил: " + "; ".join(bad))
    if not good:
        return
    await m.answer(f"👌 Получил {len(good)} {_plural(len(good))}", reply_markup=ReplyKeyboardRemove())
    await submit_manual(m, uid, d["ranges"] + good)


@dp.callback_query(F.data.startswith("len:"))
async def length_cb(c: CallbackQuery) -> None:
    uid = c.from_user.id
    d = _draft(uid)
    if not d or d.get("step") != "end":
        await c.answer("Сначала укажи начало фрагмента", show_alert=True)
        return
    await c.answer()
    await _hide_buttons(c.message)
    start_t = d.pop("cur_start")
    await add_ranges(c.message, uid, d, [(start_t, start_t + int(c.data[4:]))])


@dp.callback_query(F.data.startswith("frag:"))
async def frag_cb(c: CallbackQuery) -> None:
    uid = c.from_user.id
    d = _draft(uid)
    if not d:
        await c.answer("Пришли видео ещё раз", show_alert=True)
        return
    await c.answer()
    await _hide_buttons(c.message)
    action = c.data[5:]
    if action == "go" and d["ranges"]:
        await c.message.answer("Поехали! 🚀", reply_markup=ReplyKeyboardRemove())
        await submit_manual(d["msg"], uid, d["ranges"])
    elif action == "more":
        d.update(step="start", time=time.time())
        await c.message.answer(f"<b>Фрагмент {len(d['ranges']) + 1}.</b> Время начала?", parse_mode="HTML",
                               reply_markup=manual_keyboard(uid, d))
    elif action == "undo":
        if d["ranges"]:
            d["ranges"].pop()
        if d["ranges"]:
            text, kb = summary(uid, d)
            await c.message.answer(text, reply_markup=kb)
        else:
            d["step"] = "start"
            await c.message.answer("<b>Фрагмент 1.</b> Время начала?", parse_mode="HTML")
    elif action == "cancel":
        drafts.pop(uid, None)
        await c.message.answer("Отменил. Присылай новое видео 🎬", reply_markup=ReplyKeyboardRemove())


def _in_manual(m: Message) -> bool:
    d = _draft(m.from_user.id)
    return d is not None and d.get("step") is not None


@dp.message(F.text, _in_manual)
async def on_manual_text(m: Message) -> None:
    """Пошаговый ввод в чате: начало -> конец -> ещё или нарезать. Можно и сразу диапазоны."""
    uid = m.from_user.id
    d = drafts[uid]
    if has_timecodes(m.text):
        ranges, errors = parse_ranges(m.text, d.get("duration") or None)
        if errors:
            await m.answer("⚠️ " + "\n⚠️ ".join(errors))
        await add_ranges(m, uid, d, ranges)
        return
    t = parse_single(m.text)
    if t is None:
        await m.answer("Не понял время 🤔 Напиши, например, <code>11:24</code> или <code>1:02:30</code>.\n"
                       "Отменить — /cancel", parse_mode="HTML")
        return
    if d.get("duration") and t >= d["duration"]:
        await m.answer(f"Видео длится всего {fmt(d['duration'])} 🤔")
        return
    if d["step"] == "start":
        d.update(step="end", cur_start=t, time=time.time())
        await m.answer(f"Начало: <b>{fmt(t)}</b>. Теперь время конца — или выбери длину:",
                       parse_mode="HTML", reply_markup=length_keyboard())
        return
    start_t = d["cur_start"]
    if err := check_range(start_t, t):
        await m.answer(f"⚠️ {fmt(start_t)}–{fmt(t)}: {err}. Пришли другое время конца.")
        return
    d.pop("cur_start")
    await add_ranges(m, uid, d, [(start_t, t)])


@dp.message(F.text, lambda m: has_timecodes(m.text))
async def on_timecodes_without_video(m: Message) -> None:
    await m.answer("Сначала пришли видео или ссылку на YouTube, потом тайминги 🎬")


@dp.message()
async def fallback(m: Message) -> None:
    await m.answer("Пришли видеофайл или ссылку на YouTube 🎬")


def _plural(n: int) -> str:
    a, b = n % 10, n % 100
    if a == 1 and b != 11:
        return "фрагмент"
    if 2 <= a <= 4 and not 10 <= b < 20:
        return "фрагмента"
    return "фрагментов"


# ------------------------------------------------------------- очередь ------

async def enqueue(m: Message, kind: str, payload: str,
                  ranges: list[tuple[float, float]] | None) -> None:
    uid = m.from_user.id
    if pending.get(uid, 0) >= MAX_QUEUE_PER_USER and not is_admin(uid):
        await m.answer("⏳ Дождись, пока обработаются предыдущие видео.")
        return
    # списываем лимит сразу, чтобы его нельзя было обойти очередью; при ошибке вернём
    charge = {"fragments": len(ranges)} if ranges else {"auto": 1}
    storage.add_usage(uid, **charge)
    pending[uid] = pending.get(uid, 0) + 1
    await queue.put((m, kind, payload, ranges, charge))
    pos = queue.qsize()
    await m.answer("✅ Принял! " + (f"Ты в очереди: {pos}-й." if pos > 1 else "Начинаю обработку..."))


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


async def process(m: Message, kind: str, payload: str, ranges: list[tuple[float, float]] | None,
                  charge: dict) -> None:
    uid = m.from_user.id
    loop = asyncio.get_running_loop()
    status = await m.answer("⏬ Скачиваю видео..." if not (ranges and kind == "youtube")
                            else f"⏬ Скачиваю только нужные куски ({len(ranges)})...")

    def progress(text: str) -> None:  # вызывается из рабочего потока
        asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

    work = tempfile.mkdtemp(prefix="job_")
    s = settings_for(uid)
    style = {"unique": s["unique"], "layout": s["layout"], "subtitles": bool(s["subs"]),
             "progress": progress}
    done = False
    try:
        fragments, errors = None, []
        if ranges and kind == "youtube":
            try:
                fragments, errors = await asyncio.to_thread(download_youtube_sections, payload, ranges, work)
            except Exception:  # noqa: BLE001 — не вышло кусками, качаем целиком
                logging.exception("Не удалось скачать куски, качаю видео целиком")
                await _safe_edit(status, "⏬ Скачиваю видео целиком...")

        if fragments is None:
            if kind == "file":
                src = os.path.join(work, "source.mp4")
                await m.bot.download(payload, destination=src)
            else:
                src = await asyncio.to_thread(download_youtube, payload, work)
            info = await asyncio.to_thread(probe, src)
            if ranges:
                ok, errors = fit_to_duration(ranges, info["duration"])
                fragments = [{"video": src, "start": a, "end": b} for a, b in ok]
            elif info["duration"] > MAX_VIDEO_MIN * 60:
                await _safe_edit(status, f"😔 Видео длиннее {MAX_VIDEO_MIN} минут.")
                return

        if errors:
            await m.answer("⚠️ " + "\n⚠️ ".join(errors))
        if fragments is not None and len(fragments) < len(ranges):  # вернём лимит за выпавшие куски
            storage.add_usage(uid, fragments=-(len(ranges) - len(fragments)))
            charge = {"fragments": len(fragments)}
        if fragments is not None and not fragments:
            await _safe_edit(status, "😔 Ни один тайминг не попал внутрь видео.")
            return

        out = os.path.join(work, "out")
        if fragments is not None:
            results = await asyncio.to_thread(cut_fragments, fragments, out, **style)
        else:
            results = await asyncio.to_thread(make_shorts, src, out, n_clips=s["clips"], **style)

        await _safe_edit(status, f"📤 Отправляю {len(results)} шортс(ов)...")
        for r in results:
            icon = "✂️" if fragments is not None else "🎬"
            caption = f"{icon} {r['title']}\n⏱ {fmt(r['start'])}–{fmt(r['end'])}"
            await m.answer_video(FSInputFile(r["path"]), caption=caption, supports_streaming=True,
                                 width=r["width"], height=r["height"],
                                 duration=round(r["end"] - r["start"]))
        done = True
        tail = ""
        if not has_premium(uid):
            tail = f"\n✂️ Бесплатных фрагментов на сегодня: {fragments_left(uid)}"
        await _safe_edit(status, "✅ Готово! Присылай следующее видео." + tail)
        await reward_referrer(m.bot, uid)
    except Exception as e:  # noqa: BLE001
        logging.exception("Ошибка обработки")
        await _safe_edit(status, f"❌ Не получилось: {str(e)[:300]}\nЛимит за это видео вернул.")
    finally:
        if not done:
            storage.add_usage(uid, **{k: -v for k, v in charge.items()})
        shutil.rmtree(work, ignore_errors=True)  # диск маленький — сразу всё удаляем


async def reward_referrer(bot: Bot, uid: int) -> None:
    """Друг сделал первую нарезку — пригласившему положен Премиум."""
    referrer = storage.reward_referrer(uid)
    if not referrer:
        return
    until = storage.add_premium(referrer, REF_PREMIUM_DAYS)
    try:
        await bot.send_message(referrer, f"🎉 Твой друг сделал первую нарезку! "
                                         f"+{REF_PREMIUM_DAYS} дн. Премиума — до {until:%d.%m.%Y}.\n"
                                         "Теперь доступна 🤖 авто-нарезка.")
    except Exception:  # noqa: BLE001
        pass


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass


async def worker() -> None:
    """Обрабатываем по одной задаче за раз — слабому серверу так легче."""
    while True:
        m, kind, payload, ranges, charge = await queue.get()
        try:
            await process(m, kind, payload, ranges, charge)
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
    session = None
    if BOT_API_URL:
        extra = {}
        if BOT_API_SERVER_DIR and BOT_API_LOCAL_DIR:
            extra["wrap_local_file"] = SimpleFilesPathWrapper(Path(BOT_API_SERVER_DIR),
                                                              Path(BOT_API_LOCAL_DIR))
        session = AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_URL, is_local=True, **extra))
    bot = Bot(BOT_TOKEN, session=session)
    await bot.set_my_commands([
        BotCommand(command="start", description="Как пользоваться"),
        BotCommand(command="limit", description="Остаток на сегодня"),
        BotCommand(command="ref", description="Пригласить друга и получить Премиум"),
        BotCommand(command="premium", description="Премиум и авто-нарезка"),
        BotCommand(command="settings", description="Настройки нарезки"),
        BotCommand(command="cancel", description="Отменить разметку"),
    ])
    asyncio.create_task(worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
