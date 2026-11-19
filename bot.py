"""Telegram-бот поверх ядра shorts/. Запуск: python bot.py

Режимы:
  ✂️ Свои тайминги — бесплатно, с дневным лимитом фрагментов
  🤖 Авто-нарезка  — только для премиума
Премиум можно получить за приглашённых друзей (временный) или выдать командой /give.
"""
import asyncio
import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, WebAppInfo)
from dotenv import load_dotenv

load_dotenv()

import payments  # noqa: E402
import storage  # noqa: E402 — путь к базе берётся из .env
from shorts import config  # noqa: E402
from shorts.pipeline import ServiceNotReady, cut_fragments, make_shorts  # noqa: E402
from shorts import storyboard  # noqa: E402
from shorts.render import probe  # noqa: E402
from shorts.subtitles import POSITIONS, STYLES  # noqa: E402
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
# Папка файлов Bot API сервера (его --dir), если он запущен без Docker, например /var/lib/telegram-bot-api.
# Сервер хранит каждый полученный файл вечно — бот удаляет свою копию сразу и чистит старые файлы.
BOT_API_DIR = os.getenv("BOT_API_DIR", "") or BOT_API_LOCAL_DIR
BOT_API_FILE_TTL_MIN = max(10, _env_int("BOT_API_FILE_TTL_MIN", 60))
# Адрес окна разметки таймингов (docs/timecodes.html на GitHub Pages). Пусто — только ввод в чате
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

FREE_FRAGMENTS_PER_DAY = _env_int("FREE_FRAGMENTS_PER_DAY", 5)      # бесплатно: фрагментов в день
PREMIUM_FRAGMENTS_PER_DAY = _env_int("PREMIUM_FRAGMENTS_PER_DAY", 0)  # премиум: 0 — без лимита
PREMIUM_AUTO_PER_DAY = _env_int("PREMIUM_AUTO_PER_DAY", 5)          # премиум: авто-нарезок в день
REF_PREMIUM_DAYS = _env_int("REF_PREMIUM_DAYS", 3)  # дней премиума за приглашённого активного друга
FOUNDERS_LIMIT = _env_int("FOUNDERS_LIMIT", 300)          # первые N пользователей — «основатели»
FOUNDERS_PREMIUM_DAYS = _env_int("FOUNDERS_PREMIUM_DAYS", 7)  # их подарок: дней Премиума
REF_FRIEND_BONUS = _env_int("REF_FRIEND_BONUS", 5)  # приглашённому: +фрагментов на первые сутки
WATERMARK_BONUS = _env_int("WATERMARK_BONUS", 3)    # +фрагментов в день, если включил водяной знак
REMIND_HOUR = _env_int("REMIND_HOUR", 10)           # во сколько напоминать о лимите (местное время)
TZ_OFFSET = _env_int("TZ_OFFSET", 6)                # часовой пояс относительно UTC (Омск = 6)
MAX_VIDEO_MIN = _env_int("MAX_VIDEO_MIN", 90)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

YT_RE = re.compile(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+")
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})")
MAX_QUEUE_PER_USER = 2  # сколько задач один человек может держать в очереди
DRAFT_TTL = 3600        # через час недоделанную разметку забываем
CACHE_DIR = os.path.join(tempfile.gettempdir(), "shorts_cache")  # скачанные для раскадровки видео
local_files: dict[str, tuple[str, float]] = {}  # {file_id: (путь, когда скачан)}
broadcasts: dict[int, tuple[int, int]] = {}     # {admin_id: (chat_id, message_id)} — ждут подтверждения

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
    return max(0, min(MAX_FRAGMENTS, daily_limit(uid) - storage.used_today(uid)["fragments"]))


def daily_limit(uid: int) -> int:
    """Сколько фрагментов в день доступно человеку с учётом всех бонусов."""
    if storage.is_premium(uid):
        return PREMIUM_FRAGMENTS_PER_DAY if PREMIUM_FRAGMENTS_PER_DAY > 0 else 10 ** 6
    limit = FREE_FRAGMENTS_PER_DAY + storage.bonus_fragments(uid)
    if settings_for(uid).get("wm"):
        limit += WATERMARK_BONUS
    return limit


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
        f"А пока — ✂️ «Свои тайминги» бесплатно: {FREE_FRAGMENTS_PER_DAY} фрагментов каждый день.",
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💳 Купить Премиум", callback_data="buy:menu")]]))


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=payments.plan_title(days, price), callback_data=f"buy:{days}")]
        for days, price in payments.PLANS])


async def limit_reached(m: Message, uid: int) -> None:
    link = await ref_link(m.bot, uid)
    rows = []
    if not storage.get_user(uid)["remind"]:
        rows.append([InlineKeyboardButton(text="🔔 Напомнить, когда лимит обновится",
                                          callback_data="remind:1")])
    if not settings_for(uid).get("wm") and WATERMARK_BONUS > 0:
        rows.append([InlineKeyboardButton(text=f"💧 +{WATERMARK_BONUS} фрагм. в день за водяной знак",
                                          callback_data="set:wm:1")])
    rows.append([InlineKeyboardButton(text="📤 Пригласить друга", url=share_url(link))])
    await m.answer(
        "⏳ Бесплатные фрагменты на сегодня закончились — завтра будут новые!\n\n"
        f"🎁 Не хочешь ждать? Пригласи друга и получи {REF_PREMIUM_DAYS} дн. Премиума "
        f"с безлимитной нарезкой:\n{link}", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def share_url(link: str) -> str:
    """Ссылка «Поделиться»: Telegram откроет выбор чата с готовым текстом."""
    text = ("Нашёл бота, который режет длинные видео на шортсы с субтитрами 🔥 "
            f"По ссылке — +{REF_FRIEND_BONUS} бесплатных фрагментов")
    return f"https://t.me/share/url?url={quote(link)}&text={quote(text)}"


# ------------------------------------------------------------- настройки ----

SETTING_DEFAULTS = {"unique": "light", "layout": "blur", "clips": config.DEFAULT_CLIPS, "subs": 1,
                    "style": "classic", "pos": "bottom", "hook": 1, "wm": 0}
SETTING_VALUES = {"unique": ("none", "light", "strong"), "layout": ("blur", "crop"),
                  "clips": ("3", "5", "8"), "subs": ("0", "1"), "style": tuple(STYLES),
                  "pos": tuple(POSITIONS), "hook": ("0", "1"), "wm": ("0", "1")}
INT_SETTINGS = ("clips", "subs", "hook", "wm")


def settings_for(uid: int) -> dict:
    s = storage.get_settings(uid)
    for key, value in SETTING_DEFAULTS.items():
        s.setdefault(key, value)
    return s


def settings_text(uid: int) -> str:
    wm = (f"💧 Водяной знак — маленькая надпись с ботом в углу. За него +{WATERMARK_BONUS} "
          "бесплатных фрагмента в день." if WATERMARK_BONUS > 0 and not has_premium(uid) else "")
    return ("⚙️ <b>Настройки нарезки</b>\n\n"
            "🪝 Хук — крупный заголовок на первые 3 секунды, его придумывает ИИ.\n"
            "✂️ «Обрезка» сама держит лицо в центре кадра.\n" + wm)


def settings_keyboard(s: dict, uid: int | None = None) -> InlineKeyboardMarkup:
    def btn(text: str, key: str, value) -> InlineKeyboardButton:
        mark = "✅ " if s[key] == value else ""
        return InlineKeyboardButton(text=mark + text, callback_data=f"set:{key}:{value}")

    def toggle(text: str, key: str) -> InlineKeyboardButton:
        on = bool(s[key])
        return InlineKeyboardButton(text=("✅ " if on else "▫️ ") + text,
                                    callback_data=f"set:{key}:{0 if on else 1}")

    styles = [btn(v["title"], "style", k) for k, v in STYLES.items()]
    rows = [
        [InlineKeyboardButton(text="— Стиль субтитров —", callback_data="noop")],
        styles[:3], styles[3:],
        [btn("Внизу", "pos", "bottom"), btn("По центру", "pos", "middle")],
        [toggle("💬 Субтитры", "subs"), toggle("🪝 Хук", "hook")],
        [InlineKeyboardButton(text="— Видео —", callback_data="noop")],
        [btn("Размытый фон", "layout", "blur"), btn("Обрезка", "layout", "crop")],
        [btn("Без уникализации", "unique", "none"), btn("Лёгкая", "unique", "light"),
         btn("Сильная", "unique", "strong")],
        [btn("Авто: 3 шортса", "clips", 3), btn("5", "clips", 5), btn("8", "clips", 8)],
        [toggle(f"💧 Водяной знак (+{WATERMARK_BONUS}/день)" if WATERMARK_BONUS > 0 else "💧 Водяной знак",
                "wm")],
    ]
    if uid is not None:
        remind = bool(storage.get_user(uid)["remind"])
        rows.append([InlineKeyboardButton(text=("✅ " if remind else "▫️ ") + "🔔 Напоминать о новом лимите",
                                          callback_data=f"remind:{0 if remind else 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------- команды ----

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject | None = None) -> None:
    uid = m.from_user.id
    args = (command.args or "") if command else ""
    referrer = int(args[4:]) if args.startswith("ref_") and args[4:].isdigit() else None
    if referrer is not None and (referrer == uid or not storage.exists(referrer)):
        referrer = None  # приглашение от несуществующего пользователя не считаем
    is_new = storage.register(uid, referrer)
    storage.touch(uid)
    await m.answer(
        "👋 Привет! Я режу длинные видео на вертикальные шортсы с субтитрами.\n\n"
        "1️⃣ Пришли видео файлом или ссылку на YouTube\n"
        "2️⃣ Отметь, какие куски вырезать — в удобном окне или прямо в чате\n"
        "3️⃣ Получи готовые шортсы 🎬\n\n"
        f"✂️ Бесплатно: {FREE_FRAGMENTS_PER_DAY} фрагментов каждый день\n"
        "👑 Премиум: авто-нарезка — бот сам найдёт лучшие моменты\n\n"
        "/limit — остаток на сегодня · /ref — пригласить друга · /settings — настройки\n\n"
        "Загружай только видео, на которые у тебя есть права.")
    if not is_new:
        return
    if referrer and REF_FRIEND_BONUS > 0:
        storage.give_bonus(uid, REF_FRIEND_BONUS)
        await m.answer(f"🎁 Тебя пригласил друг — держи <b>+{REF_FRIEND_BONUS} фрагментов</b> "
                       "на первые сутки!", parse_mode="HTML")
    number = storage.make_founder(uid, FOUNDERS_LIMIT) if FOUNDERS_PREMIUM_DAYS > 0 else None
    if number:
        until = storage.add_premium(uid, FOUNDERS_PREMIUM_DAYS)
        await m.answer(
            "🏆 <b>Ты в числе первых!</b>\n\n"
            f"Ты — <b>№{number}</b> из первых {FOUNDERS_LIMIT} пользователей бота. "
            "Спасибо, что пришёл так рано 🙌\n\n"
            f"🎁 Дарим тебе <b>{FOUNDERS_PREMIUM_DAYS} дней Премиума</b> — до {until:%d.%m}:\n"
            "🤖 авто-нарезка: бот сам найдёт лучшие моменты\n"
            "✂️ нарезка по своим таймингам без дневного лимита\n\n"
            f"Осталось мест среди первых: {max(0, FOUNDERS_LIMIT - number)}. "
            "Расскажи друзьям, пока места есть 😉", parse_mode="HTML")


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
                     f"из {daily_limit(uid)}")
        if storage.bonus_fragments(uid):
            lines.append(f"🎁 Из них бонус от друга: +{storage.bonus_fragments(uid)}")
        if settings_for(uid).get("wm") and WATERMARK_BONUS > 0:
            lines.append(f"💧 За водяной знак: +{WATERMARK_BONUS}")
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


@dp.message(Command("buy"))
async def buy_cmd(m: Message) -> None:
    await m.answer("👑 Выбери тариф Премиума:", reply_markup=plans_keyboard())


@dp.callback_query(F.data.startswith("buy:"))
async def buy_cb(c: CallbackQuery) -> None:
    await c.answer()
    if c.data == "buy:menu":
        await c.message.answer("👑 Выбери тариф Премиума:", reply_markup=plans_keyboard())
        return
    plan = next(((d, p) for d, p in payments.PLANS if str(d) == c.data[4:]), None)
    if not plan:
        return
    storage.log_event("buy_click")  # считаем интерес, даже пока оплата не подключена
    if payments.ready():
        await payments.start_payment(c.bot, c.message.chat.id, c.from_user.id, *plan)
        return
    # ЗАГЛУШКА: оплата ещё не подключена
    await c.message.answer(
        "💳 Оплата подключается и появится совсем скоро!\n\n"
        f"А пока Премиум можно получить бесплатно — {REF_PREMIUM_DAYS} дн. за каждого друга: /ref")


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
                   f"⏳ В очереди: {queue.qsize()}\n"
                   f"💳 Нажали «Купить»: сегодня {s['buy_clicks_today']}, всего {s['buy_clicks']}\n"
                   f"🏆 Основателей: {s['founders']} из {FOUNDERS_LIMIT}\n"
                   f"🔔 С напоминаниями: {s['remind']}\n🚫 Заблокировали бота: {s['blocked']}\n"
                   f"❗ Ошибок за сутки: {s['errors_24h']}" + (" — /errors" if s['errors_24h'] else "") + "\n\n"
                   + services_text())


@dp.message(Command("errors"))
async def errors_cmd(m: Message) -> None:
    """Админ: последние ошибки бота."""
    if not is_admin(m.from_user.id):
        return
    rows = storage.recent_errors(8)
    if not rows:
        await m.answer("✅ Ошибок нет")
        return
    parts = []
    for r in rows:
        when = dt.datetime.fromisoformat(r["time"]) + dt.timedelta(hours=TZ_OFFSET)
        parts.append(f"🕐 {when:%d.%m %H:%M}\n{r['text'][-600:]}")
    await m.answer("❗ Последние ошибки (новые сверху):\n\n" + "\n\n".join(parts)[-4000:])


@dp.message(Command("broadcast"))
async def broadcast_cmd(m: Message) -> None:
    """Админ: ответь этой командой на сообщение — бот разошлёт его всем (после подтверждения)."""
    if not is_admin(m.from_user.id):
        return
    src = m.reply_to_message
    if not src:
        await m.answer("📣 Напиши сообщение для рассылки (можно с фото или видео), "
                       "потом ответь на него командой /broadcast.")
        return
    broadcasts[m.from_user.id] = (src.chat.id, src.message_id)
    total = len(storage.broadcast_targets())
    await m.answer(f"📣 Разослать это сообщение {total} пользователям?",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                       InlineKeyboardButton(text=f"✅ Отправить ({total})", callback_data="bc:go"),
                       InlineKeyboardButton(text="✖️ Отмена", callback_data="bc:cancel")]]))


@dp.callback_query(F.data.startswith("bc:"))
async def broadcast_cb(c: CallbackQuery) -> None:
    uid = c.from_user.id
    if not is_admin(uid):
        await c.answer()
        return
    await c.answer()
    await _hide_buttons(c.message)
    src = broadcasts.pop(uid, None)
    if c.data == "bc:cancel" or not src:
        await c.message.answer("Рассылка отменена.")
        return
    await c.message.answer("📣 Рассылка началась, пришлю отчёт.")
    asyncio.create_task(run_broadcast(c.bot, uid, *src))


async def run_broadcast(bot: Bot, admin_id: int, chat_id: int, message_id: int) -> None:
    sent = blocked = failed = 0
    for target in storage.broadcast_targets():
        for _ in range(3):
            try:
                await bot.copy_message(target, chat_id, message_id)
                sent += 1
                break
            except TelegramRetryAfter as e:  # Telegram просит подождать
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                storage.set_blocked(target)
                blocked += 1
                break
            except Exception:  # noqa: BLE001
                failed += 1
                break
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду
    await bot.send_message(admin_id, f"📣 Рассылка готова\n✅ Доставлено: {sent}\n"
                                     f"🚫 Заблокировали бота: {blocked}\n⚠️ Ошибки: {failed}")


def services_text() -> str:
    """Что из внешних сервисов подключено, а где стоит заглушка."""
    def row(ok: bool, name: str, stub: str) -> str:
        return f"{'✅' if ok else '⏸'} {name}" + ("" if ok else f" — заглушка: {stub}")
    return "🔌 Сервисы:\n" + "\n".join([
        row(config.speech_ready(), "Распознавание речи (Groq)", "без субтитров, авто выключено"),
        row(config.llm_ready(), "ИИ выбор моментов", "простой выбор по плотности речи"),
        row(payments.ready(), "Оплата", "кнопка «Купить» только считает интерес"),
        row(bool(WEBAPP_URL), "Окно разметки", "тайминги вводятся в чате"),
        row(bool(BOT_API_URL), "Local Bot API", "файлы до 20 МБ"),
    ])


@dp.message(Command("settings"))
async def settings_cmd(m: Message) -> None:
    uid = m.from_user.id
    await m.answer(settings_text(uid), parse_mode="HTML",
                   reply_markup=settings_keyboard(settings_for(uid), uid))


@dp.callback_query(F.data.startswith("set:"))
async def settings_cb(c: CallbackQuery) -> None:
    _, key, value = c.data.split(":", 2)
    uid = c.from_user.id
    s = settings_for(uid)
    if value not in SETTING_VALUES.get(key, ()):
        await c.answer()
        return
    s[key] = int(value) if key in INT_SETTINGS else value
    storage.save_settings(uid, s)
    note = "Сохранено"
    if key == "wm" and s["wm"] and WATERMARK_BONUS > 0 and not has_premium(uid):
        note = f"💧 +{WATERMARK_BONUS} фрагмента в день — уже сегодня!"
    try:
        if c.message.text and c.message.text.startswith("⚙️"):
            await c.message.edit_reply_markup(reply_markup=settings_keyboard(s, uid))
    except TelegramBadRequest:
        pass
    await c.answer(note, show_alert=key == "wm" and bool(s["wm"]))


@dp.callback_query(F.data.startswith("remind:"))
async def remind_cb(c: CallbackQuery) -> None:
    on = c.data == "remind:1"
    storage.set_remind(c.from_user.id, on)
    await c.answer(f"🔔 Напомню в {REMIND_HOUR}:00, когда лимит обновится" if on else "Напоминания выключены",
                   show_alert=on)
    try:
        if c.message.text and c.message.text.startswith("⚙️"):
            await c.message.edit_reply_markup(
                reply_markup=settings_keyboard(settings_for(c.from_user.id), c.from_user.id))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "noop")
async def noop_cb(c: CallbackQuery) -> None:
    await c.answer()


@dp.message(Command("cancel"))
async def cancel_cmd(m: Message) -> None:
    n = await cancel_all(m.from_user.id)
    await m.answer(("🛑 Отменил всё, лимит вернул.\n" if n else "Отменять нечего. ") + "Присылай новое видео 🎬",
                   reply_markup=ReplyKeyboardRemove())


@dp.callback_query(F.data == "job:cancel")
async def job_cancel_cb(c: CallbackQuery) -> None:
    n = await cancel_all(c.from_user.id)
    await c.answer("🛑 Отменяю всё" if n else "Нечего отменять — всё уже закончилось")
    if n:
        await c.message.answer("🛑 Всё отменил, лимит вернул. Присылай новое видео 🎬",
                               reply_markup=ReplyKeyboardRemove())
    else:
        await _hide_buttons(c.message)


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
        if not config.speech_ready():  # ЗАГЛУШКА: ИИ ещё не подключён
            await c.message.answer("🤖 Авто-нарезка скоро заработает — подключаем ИИ.\n"
                                   "А пока нарежь по своим таймингам ✂️")
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
    if d["kind"] == "file" and storyboard.available():
        await m.answer("Не помнишь, где нужные моменты? Покажу кадры с таймкодами 👇",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                           InlineKeyboardButton(text="🖼 Раскадровка", callback_data="board")]]))


@dp.callback_query(F.data == "board")
async def board_cb(c: CallbackQuery) -> None:
    uid = c.from_user.id
    d = _draft(uid)
    if not d or d["kind"] != "file":
        await c.answer("Пришли видео ещё раз", show_alert=True)
        return
    await c.answer()
    await _hide_buttons(c.message)
    status = await c.message.answer("🖼 Готовлю раскадровку...")
    try:
        path = await cached_file(c.bot, d["payload"])
        info = await asyncio.to_thread(probe, path)
        d["duration"] = info["duration"]
        board = path + ".jpg"
        await asyncio.to_thread(storyboard.make_storyboard, path, info["duration"], board)
        await c.message.answer_photo(FSInputFile(board), caption=(
            f"🖼 Видео длится {fmt(info['duration'])}. Время каждого кадра — в углу.\n"
            "Пришли время начала фрагмента, например <code>1:20</code>"), parse_mode="HTML")
        os.remove(board)
        await status.delete()
    except Exception as e:  # noqa: BLE001
        logging.exception("Не удалось сделать раскадровку")
        await _safe_edit(status, f"😔 Не получилось сделать раскадровку: {str(e)[:200]}")


async def cached_file(bot: Bot, file_id: str) -> str:
    """Скачивает видео один раз: потом его же возьмёт нарезка, второй раз качать не нужно."""
    path, _ = local_files.get(file_id, ("", 0))
    if path and os.path.exists(path):
        return path
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{abs(hash(file_id))}_{int(time.time())}.mp4")
    await download_tg(bot, file_id, path)
    local_files[file_id] = (path, time.time())
    return path


def _bot_api_local_path(file_path: str) -> str:
    """Путь файла от Bot API сервера -> путь на этом компьютере (для сервера в Docker)."""
    if BOT_API_SERVER_DIR and BOT_API_LOCAL_DIR:
        return str(SimpleFilesPathWrapper(Path(BOT_API_SERVER_DIR), Path(BOT_API_LOCAL_DIR)).to_local(file_path))
    return file_path


def remove_bot_api_file(root: str, file_path: str | None) -> None:
    """Удаляет копию файла у Local Bot API сервера — только внутри его папки."""
    if not root or not file_path:
        return
    try:
        p = Path(_bot_api_local_path(file_path)).resolve()
        p.relative_to(Path(root).resolve())
        if p.is_file():
            p.unlink()
    except (OSError, ValueError):
        pass


def cleanup_bot_api_dir(root: str, ttl_minutes: int) -> int:
    """Удаляет старые файлы из папки Bot API сервера: <папка>/<токен бота>/<тип>/..."""
    base = Path(root) if root else None
    if base is None or not base.is_dir():
        return 0
    cutoff = time.time() - ttl_minutes * 60
    removed = 0
    for token_dir in base.iterdir():
        if not token_dir.is_dir() or ":" not in token_dir.name:
            continue
        for fp in token_dir.rglob("*"):
            try:
                if fp.is_file() and not fp.is_symlink() and fp.stat().st_mtime < cutoff:
                    fp.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


async def download_tg(bot: Bot, file_id: str, destination: str) -> None:
    """Скачивает файл из Telegram и сразу удаляет копию, которую оставил у себя Local Bot API сервер."""
    f = await bot.get_file(file_id)
    await bot.download_file(f.file_path, destination=destination, timeout=900)
    if BOT_API_URL:
        remove_bot_api_file(BOT_API_DIR, f.file_path)


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

class Cancelled(Exception):
    """Человек нажал «Отменить»."""


class Job:
    """Одна задача нарезки: её можно отменить и в очереди, и во время работы."""

    def __init__(self, uid: int, charge: dict | None = None):
        self.uid = uid
        self.charge = charge or {}
        self.cancel = threading.Event()
        self.started = False   # воркер уже взял её в работу
        self.dropped = False   # отменена, пока ждала в очереди — воркер её просто пропустит
        self.status: Message | None = None


jobs: dict[int, list[Job]] = {}  # {user_id: задачи в очереди и в работе}


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отменить", callback_data="job:cancel")]])


def progress_bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    return "▓" * filled + "░" * (width - filled) + f" {round(frac * 100)}%"


class Progress:
    """Сообщение со статусом и полосой прогресса. Telegram не любит частые правки,
    поэтому внутри одного этапа обновляем не чаще раза в PROGRESS_EVERY секунд."""
    PROGRESS_EVERY = 3.0

    def __init__(self, msg: Message, loop: asyncio.AbstractEventLoop, job: Job):
        self.msg, self.loop, self.job = msg, loop, job
        self.text, self.shown, self.last = "", "", 0.0

    def set(self, text: str, frac: float | None = None) -> None:
        """Можно звать из рабочего потока. Если задачу отменили — прерывает её."""
        if self.job.cancel.is_set():
            raise Cancelled()
        now = time.monotonic()
        if text == self.text and now - self.last < self.PROGRESS_EVERY:
            return
        self.text, self.last = text, now
        body = text + (f"\n{progress_bar(frac)}" if frac is not None else "")
        if body == self.shown:
            return
        self.shown = body
        asyncio.run_coroutine_threadsafe(_safe_edit(self.msg, body, cancel_keyboard()), self.loop)

    def step(self, text: str) -> None:
        """Сообщения ядра вида «Монтирую фрагмент 2 из 3» — полоса по номеру шага."""
        found = re.search(r"(\d+) из (\d+)", text)
        frac = (int(found.group(1)) - 1) / int(found.group(2)) if found else None
        self.set(text, frac)


def ydl_hook(report) -> callable:
    """Хук yt-dlp: доля скачанного (по кускам или по байтам) -> report(доля)."""
    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        frac = None
        if d.get("fragment_count"):
            frac = (d.get("fragment_index") or 0) / d["fragment_count"]
        elif d.get("total_bytes") or d.get("total_bytes_estimate"):
            frac = (d.get("downloaded_bytes") or 0) / (d.get("total_bytes") or d["total_bytes_estimate"])
        report(frac)
    return hook


async def cancel_all(uid: int) -> int:
    """Кнопка «Отменить»: сбрасывает разметку, убирает задачи из очереди и останавливает текущую."""
    had_draft = drafts.pop(uid, None) is not None
    user_jobs = jobs.get(uid, [])
    count = len(user_jobs) + had_draft
    for job in list(user_jobs):
        job.cancel.set()
        if job.started:
            if job.status:  # саму остановку и возврат лимита сделает process()
                await _safe_edit(job.status, "🛑 Останавливаю...")
            continue
        job.dropped = True
        storage.add_usage(uid, **{k: -v for k, v in job.charge.items()})
        pending[uid] = max(0, pending.get(uid, 0) - 1)
        user_jobs.remove(job)
        if job.status:
            await _safe_edit(job.status, "🛑 Отменено, лимит вернул.")
    return count


async def enqueue(m: Message, kind: str, payload: str,
                  ranges: list[tuple[float, float]] | None) -> None:
    uid = m.from_user.id
    if pending.get(uid, 0) >= MAX_QUEUE_PER_USER and not is_admin(uid):
        await m.answer("⏳ Дождись, пока обработаются предыдущие видео — или нажми «❌ Отменить».",
                       reply_markup=cancel_keyboard())
        return
    storage.touch(uid)
    # списываем лимит сразу, чтобы его нельзя было обойти очередью; при ошибке вернём
    charge = {"fragments": len(ranges)} if ranges else {"auto": 1}
    storage.add_usage(uid, **charge)
    pending[uid] = pending.get(uid, 0) + 1
    job = Job(uid, charge)
    jobs.setdefault(uid, []).append(job)
    pos = queue.qsize() + 1
    # сначала сообщение, потом очередь: иначе воркер может взять задачу раньше, чем оно появится
    job.status = await m.answer("✅ Принял! " + (f"Ты в очереди: {pos}-й." if pos > 1 else "Начинаю обработку..."),
                                reply_markup=cancel_keyboard())
    await queue.put((m, kind, payload, ranges, charge, job))


YT_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"


def download_youtube_sections(url: str, ranges: list[tuple[float, float]], work: str,
                              report=lambda text, frac: None) -> tuple[list[dict], list[str]]:
    """Скачивает с YouTube только нужные куски, а не всё видео — быстрее и почти не занимает диск."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    ranges, errors = fit_to_duration(ranges, float(info.get("duration") or 0) or float("inf"))
    fragments = []
    n = len(ranges)
    for i, (start, end) in enumerate(ranges, 1):
        text = f"⏬ Скачиваю кусок {i} из {n}"
        report(text, (i - 1) / n)
        opts = {
            "format": YT_FORMAT,
            "outtmpl": os.path.join(work, f"part_{i:02d}.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True, "noprogress": True, "noplaylist": True,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,  # точный рез, а не по ближайшему ключевому кадру
            "progress_hooks": [ydl_hook(lambda f, i=i, text=text: report(text, (i - 1 + (f or 0)) / n))],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = sorted(glob.glob(os.path.join(work, f"part_{i:02d}.*")))
        if not files:
            raise ValueError(f"не удалось скачать фрагмент {fmt(start)}-{fmt(end)}")
        fragments.append({"video": files[0], "start": 0, "end": end - start,
                          "orig_start": start, "orig_end": end})
    return fragments, errors


def download_youtube(url: str, work: str, report=lambda text, frac: None) -> str:
    import yt_dlp
    opts = {
        "format": YT_FORMAT,
        "outtmpl": os.path.join(work, "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True, "noprogress": True, "noplaylist": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_VIDEO_MIN * 60}"),
        "progress_hooks": [ydl_hook(lambda f: report("⏬ Скачиваю видео", f))],
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
                  charge: dict, job: Job | None = None) -> None:
    uid = m.from_user.id
    loop = asyncio.get_running_loop()
    job = job or Job(uid, charge)
    first = ("⏬ Скачиваю видео..." if not (ranges and kind == "youtube")
             else f"⏬ Скачиваю только нужные куски ({len(ranges)})...")
    status = job.status or await m.answer(first, reply_markup=cancel_keyboard())
    bar = Progress(status, loop, job)
    bar.set(first)
    progress = bar.step  # сообщения ядра: «Монтирую фрагмент 2 из 3» и т.п.

    work = tempfile.mkdtemp(prefix="job_")
    s = settings_for(uid)
    me = await m.bot.me()
    style = {"unique": s["unique"], "layout": s["layout"], "subtitles": bool(s["subs"]),
             "style": s["style"], "position": s["pos"], "hook": bool(s["hook"]),
             "watermark": f"@{me.username}" if s["wm"] else "", "progress": progress}
    done = False
    try:
        fragments, errors = None, []
        if ranges and kind == "youtube":
            try:
                fragments, errors = await asyncio.to_thread(download_youtube_sections, payload, ranges,
                                                            work, bar.set)
            except Exception:  # noqa: BLE001 — не вышло кусками, качаем целиком
                if job.cancel.is_set():
                    raise Cancelled()
                logging.exception("Не удалось скачать куски, качаю видео целиком")
                bar.set("⏬ Скачиваю видео целиком...")

        if fragments is None:
            if kind == "file":
                src = os.path.join(work, "source.mp4")
                cached, _ = local_files.pop(payload, ("", 0))
                if cached and os.path.exists(cached):
                    shutil.move(cached, src)  # уже скачано для раскадровки
                else:
                    await download_tg(m.bot, payload, src)
            else:
                src = await asyncio.to_thread(download_youtube, payload, work, bar.set)
            bar.set("🔍 Проверяю видео...")
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

        for i, r in enumerate(results, 1):
            bar.step(f"📤 Отправляю шортс {i} из {len(results)}...")
            icon = "✂️" if fragments is not None else "🎬"
            caption = f"{icon} {r['title']}\n⏱ {fmt(r['start'])}–{fmt(r['end'])}"
            await m.answer_video(FSInputFile(r["path"]), caption=caption, supports_streaming=True,
                                 width=r["width"], height=r["height"],
                                 duration=round(r["end"] - r["start"]))
        done = True
        tail = ""
        if s["subs"] and not config.speech_ready():  # ЗАГЛУШКА
            tail += "\n💬 Субтитры появятся, когда подключим распознавание речи."
        if not has_premium(uid):
            tail += f"\n✂️ Бесплатных фрагментов на сегодня: {fragments_left(uid)}"
        link = await ref_link(m.bot, uid)
        await _safe_edit(status, "✅ Готово! Присылай следующее видео." + tail,
                         InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="📤 Поделиться ботом с другом", url=share_url(link))]]))
        await reward_referrer(m.bot, uid)
    except Cancelled:
        await _safe_edit(status, "🛑 Отменено, лимит вернул.")
    except ServiceNotReady:
        await _safe_edit(status, "🤖 Эта функция скоро заработает — подключаем ИИ. Лимит вернул.")
    except Exception as e:  # noqa: BLE001
        if job.cancel.is_set():  # yt-dlp мог обернуть нашу отмену в свою ошибку
            await _safe_edit(status, "🛑 Отменено, лимит вернул.")
        else:
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


async def _safe_edit(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


async def worker() -> None:
    """Обрабатываем по одной задаче за раз — слабому серверу так легче."""
    while True:
        m, kind, payload, ranges, charge, job = await queue.get()
        uid = m.from_user.id
        if job.dropped:  # отменили, пока ждала: лимит и очередь cancel_all уже поправил
            queue.task_done()
            continue
        job.started = True
        try:
            await process(m, kind, payload, ranges, charge, job)
        except Exception:  # noqa: BLE001 — очередь не должна останавливаться из-за одной ошибки
            logging.exception("Сбой в обработчике очереди")
        finally:
            pending[uid] = max(0, pending.get(uid, 0) - 1)
            if job in jobs.get(uid, []):
                jobs[uid].remove(job)
            queue.task_done()


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)


async def send_reminders(bot: Bot) -> int:
    """Напоминаем тем, кто сам включил: лимит обновился. Возвращает, скольким отправили."""
    sent = 0
    off = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔕 Не напоминать", callback_data="remind:0")]])
    for uid in storage.reminder_targets():
        storage.mark_reminded(uid)
        if has_premium(uid):
            continue  # у Премиума лимита нет — напоминать не о чем
        try:
            await bot.send_message(uid, f"🔄 Лимит обновился! Сегодня снова {daily_limit(uid)} "
                                        "бесплатных фрагментов ✂️\nПрисылай видео 🎬", reply_markup=off)
            sent += 1
        except TelegramForbiddenError:
            storage.set_blocked(uid)
        except Exception:  # noqa: BLE001
            logging.exception("Не удалось отправить напоминание")
        await asyncio.sleep(0.05)
    return sent


def clean_cache(max_age: float = 2 * DRAFT_TTL) -> None:
    """Удаляем видео, скачанные для раскадровки и так и не нарезанные."""
    now = time.time()
    for file_id, (path, when) in list(local_files.items()):
        if now - when > max_age:
            local_files.pop(file_id, None)
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass


async def background(bot: Bot) -> None:
    """Раз в 10 минут: напоминания о лимите (после REMIND_HOUR) и уборка временных файлов."""
    while True:
        try:
            if local_now().hour >= REMIND_HOUR:
                await send_reminders(bot)
            clean_cache()
            if BOT_API_URL and BOT_API_DIR:
                removed = await asyncio.to_thread(cleanup_bot_api_dir, BOT_API_DIR, BOT_API_FILE_TTL_MIN)
                if removed:
                    logging.info("Удалено старых файлов Bot API сервера: %s", removed)
        except Exception:  # noqa: BLE001
            logging.exception("Сбой фоновой задачи")
        await asyncio.sleep(600)


class DBErrorLog(logging.Handler):
    """Пишет ошибки в базу — их видно в /errors и /stats с телефона."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            storage.log_error(self.format(record))
        except Exception:  # noqa: BLE001 — журнал не должен ронять бота
            pass


_error_log = DBErrorLog(level=logging.ERROR)
_error_log.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_error_log)


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
        BotCommand(command="buy", description="Купить Премиум"),
        BotCommand(command="settings", description="Настройки нарезки"),
        BotCommand(command="cancel", description="Отменить всё: разметку и нарезку"),
    ])
    asyncio.create_task(worker())
    asyncio.create_task(background(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
