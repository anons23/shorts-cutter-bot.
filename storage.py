"""Хранилище бота на SQLite (встроено в Python, ничего ставить не нужно).

Пользователи, настройки, премиум, дневные лимиты и рефералы — всё в одном файле bot.db.
"""
import datetime as dt
import json
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "bot.db")

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
CREATE TABLE IF NOT EXISTS usage (
    user_id   INTEGER,
    day       TEXT,
    fragments INTEGER DEFAULT 0,  -- ручные фрагменты за день
    auto      INTEGER DEFAULT 0,  -- авто-нарезки за день
    PRIMARY KEY (user_id, day)
);
""")
_db.commit()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _today() -> str:
    return dt.date.today().isoformat()


# --- пользователи ---------------------------------------------------------

def register(uid: int, referrer: int | None = None) -> bool:
    """Добавляет пользователя. True — если он новый. Реферера записываем только новым."""
    if referrer == uid:
        referrer = None
    cur = _db.execute("INSERT OR IGNORE INTO users (id, first_seen, referrer) VALUES (?, ?, ?)",
                      (uid, _now().isoformat(), referrer))
    _db.commit()
    return cur.rowcount == 1


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


def stats() -> dict:
    users = _db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = _db.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?",
                          (_now().isoformat(),)).fetchone()[0]
    today = _db.execute("""SELECT COUNT(*), COALESCE(SUM(fragments), 0), COALESCE(SUM(auto), 0)
                           FROM usage WHERE day = ?""", (_today(),)).fetchone()
    return {"users": users, "premium": premium, "active_today": today[0],
            "fragments_today": today[1], "auto_today": today[2]}
