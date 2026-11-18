"""Хранилище бота на SQLite (встроено в Python, ничего ставить не нужно).

Пользователи, настройки, премиум, дневные лимиты и рефералы — всё в одном файле bot.db.
"""
import datetime as dt
import json
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "bot.db")
try:
    TZ_OFFSET = int(os.getenv("TZ_OFFSET", "6"))  # часовой пояс относительно UTC (Омск = 6)
except ValueError:
    TZ_OFFSET = 6

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    referrer      INTEGER,            -- кто пригласил
    ref_rewarded  INTEGER DEFAULT 0,  -- пригласивший уже получил награду за этого человека
    premium_until TEXT,               -- ISO-время окончания премиума
    settings      TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS errors (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT,
    text TEXT
);
CREATE TABLE IF NOT EXISTS events (
    day   TEXT,
    name  TEXT,                       -- например buy_click: нажали «Купить»
    count INTEGER DEFAULT 0,
    PRIMARY KEY (day, name)
);
CREATE TABLE IF NOT EXISTS usage (
    user_id   INTEGER,
    day       TEXT,
    fragments INTEGER DEFAULT 0,  -- ручные фрагменты за день
    auto      INTEGER DEFAULT 0,  -- авто-нарезки за день
    PRIMARY KEY (user_id, day)
);
""")
# Новые колонки добавляем и в старую базу, если она уже была создана
_NEW_COLUMNS = {
    "founder_no": "INTEGER",       # номер среди первых пользователей
    "bonus_fragments": "INTEGER DEFAULT 0",
    "bonus_until": "TEXT",         # до какого времени действует бонус приглашённому
    "remind": "INTEGER DEFAULT 0", # напоминать, что лимит обновился
    "last_reminded": "TEXT",
    "last_active": "TEXT",
    "blocked": "INTEGER DEFAULT 0",  # заблокировал бота — рассылки не шлём
}
_have = {r[1] for r in _db.execute("PRAGMA table_info(users)")}
for _name, _type in _NEW_COLUMNS.items():
    if _name not in _have:
        _db.execute(f"ALTER TABLE users ADD COLUMN {_name} {_type}")
_db.commit()

MAX_ERRORS = 300  # храним только последние ошибки, чтобы база не росла


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _today() -> str:
    """День по часовому поясу бота: лимиты обновляются в полночь по местному времени."""
    return (_now() + dt.timedelta(hours=TZ_OFFSET)).date().isoformat()


# --- пользователи ---------------------------------------------------------

def register(uid: int, referrer: int | None = None) -> bool:
    """Добавляет пользователя. True — если он новый. Реферера записываем только новым."""
    if referrer == uid:
        referrer = None
    cur = _db.execute("INSERT OR IGNORE INTO users (id, first_seen, referrer) VALUES (?, ?, ?)",
                      (uid, _now().isoformat(), referrer))
    _db.commit()
    return cur.rowcount == 1


def exists(uid: int) -> bool:
    return _db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is not None


def user_count() -> int:
    return _db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def make_founder(uid: int, limit: int) -> int | None:
    """Если человек среди первых `limit` пользователей — присваиваем номер и возвращаем его."""
    number = _db.execute("SELECT COUNT(*) FROM users WHERE rowid <= (SELECT rowid FROM users WHERE id = ?)",
                         (uid,)).fetchone()[0]
    if number > limit:
        return None
    _db.execute("UPDATE users SET founder_no = ? WHERE id = ?", (number, uid))
    _db.commit()
    return number


def touch(uid: int) -> None:
    """Отмечаем, что человек пользовался ботом (для напоминаний и статистики)."""
    _db.execute("UPDATE users SET last_active = ?, blocked = 0 WHERE id = ?", (_now().isoformat(), uid))
    _db.commit()


def get_user(uid: int) -> sqlite3.Row:
    register(uid)
    return _db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def get_settings(uid: int) -> dict:
    try:
        return json.loads(get_user(uid)["settings"] or "{}")
    except ValueError:
        return {}


def save_settings(uid: int, settings: dict) -> None:
    register(uid)
    _db.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings), uid))
    _db.commit()


# --- премиум --------------------------------------------------------------

def premium_until(uid: int) -> dt.datetime | None:
    value = get_user(uid)["premium_until"]
    if not value:
        return None
    until = dt.datetime.fromisoformat(value)
    return until if until > _now() else None


def is_premium(uid: int) -> bool:
    return premium_until(uid) is not None


def add_premium(uid: int, days: float) -> dt.datetime:
    """Продлевает премиум: от текущего окончания, если он ещё действует, иначе от сейчас."""
    start = premium_until(uid) or _now()
    until = start + dt.timedelta(days=days)
    _db.execute("UPDATE users SET premium_until = ? WHERE id = ?", (until.isoformat(), uid))
    _db.commit()
    return until


# --- бонус приглашённому -------------------------------------------------

def give_bonus(uid: int, fragments: int, hours: float = 24) -> None:
    until = _now() + dt.timedelta(hours=hours)
    _db.execute("UPDATE users SET bonus_fragments = ?, bonus_until = ? WHERE id = ?",
                (fragments, until.isoformat(), uid))
    _db.commit()


def bonus_fragments(uid: int) -> int:
    row = get_user(uid)
    if not row["bonus_until"] or dt.datetime.fromisoformat(row["bonus_until"]) <= _now():
        return 0
    return row["bonus_fragments"] or 0


# --- напоминания и рассылки ----------------------------------------------

def set_remind(uid: int, on: bool) -> None:
    register(uid)
    _db.execute("UPDATE users SET remind = ? WHERE id = ?", (int(on), uid))
    _db.commit()


def reminder_targets(active_days: int = 30) -> list[int]:
    """Кому сегодня напомнить: включили напоминания, не заблокировали бота, недавно заходили,
    сегодня ещё не напоминали и сегодня ещё ничего не резали."""
    since = (_now() - dt.timedelta(days=active_days)).isoformat()
    rows = _db.execute("""SELECT id FROM users WHERE remind = 1 AND blocked = 0
                          AND last_active >= ? AND COALESCE(last_reminded, '') != ?
                          AND id NOT IN (SELECT user_id FROM usage WHERE day = ? AND fragments > 0)""",
                       (since, _today(), _today())).fetchall()
    return [r[0] for r in rows]


def mark_reminded(uid: int) -> None:
    _db.execute("UPDATE users SET last_reminded = ? WHERE id = ?", (_today(), uid))
    _db.commit()


def set_blocked(uid: int) -> None:
    _db.execute("UPDATE users SET blocked = 1, remind = 0 WHERE id = ?", (uid,))
    _db.commit()


def broadcast_targets() -> list[int]:
    return [r[0] for r in _db.execute("SELECT id FROM users WHERE blocked = 0 ORDER BY id")]


# --- журнал ошибок --------------------------------------------------------

def log_error(text: str) -> None:
    _db.execute("INSERT INTO errors (time, text) VALUES (?, ?)", (_now().isoformat(), text[:3000]))
    _db.execute("DELETE FROM errors WHERE id <= (SELECT MAX(id) FROM errors) - ?", (MAX_ERRORS,))
    _db.commit()


def recent_errors(limit: int = 10) -> list[sqlite3.Row]:
    return _db.execute("SELECT time, text FROM errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def errors_since(hours: float = 24) -> int:
    since = (_now() - dt.timedelta(hours=hours)).isoformat()
    return _db.execute("SELECT COUNT(*) FROM errors WHERE time >= ?", (since,)).fetchone()[0]


# --- дневные лимиты -------------------------------------------------------

def used_today(uid: int) -> dict:
    row = _db.execute("SELECT fragments, auto FROM usage WHERE user_id = ? AND day = ?",
                      (uid, _today())).fetchone()
    return {"fragments": row["fragments"], "auto": row["auto"]} if row else {"fragments": 0, "auto": 0}


def add_usage(uid: int, fragments: int = 0, auto: int = 0) -> None:
    """Отрицательные числа возвращают лимит (если обработка упала)."""
    _db.execute("INSERT OR IGNORE INTO usage (user_id, day) VALUES (?, ?)", (uid, _today()))
    _db.execute("""UPDATE usage SET fragments = MAX(0, fragments + ?), auto = MAX(0, auto + ?)
                   WHERE user_id = ? AND day = ?""", (fragments, auto, uid, _today()))
    _db.commit()


# --- рефералы -------------------------------------------------------------

def reward_referrer(uid: int) -> int | None:
    """Вызывается после первой успешной нарезки. Возвращает id пригласившего, если ему положена награда."""
    row = get_user(uid)
    if not row["referrer"] or row["ref_rewarded"]:
        return None
    cur = _db.execute("UPDATE users SET ref_rewarded = 1 WHERE id = ? AND ref_rewarded = 0", (uid,))
    _db.commit()
    return row["referrer"] if cur.rowcount == 1 else None


def referral_stats(uid: int) -> dict:
    row = _db.execute("""SELECT COUNT(*) AS invited, COALESCE(SUM(ref_rewarded), 0) AS active
                         FROM users WHERE referrer = ?""", (uid,)).fetchone()
    return {"invited": row["invited"], "active": row["active"]}


def log_event(name: str) -> None:
    _db.execute("INSERT OR IGNORE INTO events (day, name) VALUES (?, ?)", (_today(), name))
    _db.execute("UPDATE events SET count = count + 1 WHERE day = ? AND name = ?", (_today(), name))
    _db.commit()


def event_count(name: str, today_only: bool = False) -> int:
    sql = "SELECT COALESCE(SUM(count), 0) FROM events WHERE name = ?" + (" AND day = ?" if today_only else "")
    return _db.execute(sql, (name, _today()) if today_only else (name,)).fetchone()[0]


def stats() -> dict:
    users = _db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = _db.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?",
                          (_now().isoformat(),)).fetchone()[0]
    today = _db.execute("""SELECT COUNT(*), COALESCE(SUM(fragments), 0), COALESCE(SUM(auto), 0)
                           FROM usage WHERE day = ?""", (_today(),)).fetchone()
    return {"users": users, "premium": premium, "active_today": today[0],
            "fragments_today": today[1], "auto_today": today[2],
            "buy_clicks_today": event_count("buy_click", True), "buy_clicks": event_count("buy_click"),
            "blocked": _db.execute("SELECT COUNT(*) FROM users WHERE blocked = 1").fetchone()[0],
            "remind": _db.execute("SELECT COUNT(*) FROM users WHERE remind = 1").fetchone()[0],
            "founders": _db.execute("SELECT COUNT(*) FROM users WHERE founder_no IS NOT NULL").fetchone()[0],
            "errors_24h": errors_since(24)}
