"""Разбор таймингов, которые пишет человек: «1:20-1:45», «0:01:05 — 0:01:40», «80-105»."""
import re

MAX_FRAGMENTS = 10      # сколько фрагментов можно указать за раз
MAX_FRAGMENT_SEC = 180  # Shorts допускают до 3 минут
MIN_FRAGMENT_SEC = 1

_TIME = r"\d{1,2}(?::\d{1,2}){1,2}(?:\.\d+)?|\d+(?:\.\d+)?"
_RANGE = re.compile(rf"(?<![\d:.])({_TIME})\s*(?:-|–|—|до)\s*({_TIME})(?![\d:])")
_URL = re.compile(r"https?://\S+")


def to_seconds(text: str) -> float:
    """«1:20» -> 80, «1:02:03» -> 3723, «95» -> 95."""
    parts = text.split(":")
    for p in parts[1:]:
        if float(p) >= 60:
            raise ValueError(f"«{text}»: минут и секунд не может быть больше 59")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def fmt(t: float) -> str:
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def has_timecodes(text: str | None) -> bool:
    return bool(text) and bool(_RANGE.search(_URL.sub(" ", text)))


def parse_ranges(text: str, duration: float | None = None) -> tuple[list[tuple[float, float]], list[str]]:
    """Возвращает (список отрезков, список ошибок понятным языком)."""
    text = _URL.sub(" ", text or "")  # чтобы цифры из ссылки (?t=90) не приняли за тайминг
    ranges, errors = [], []
    for a, b in _RANGE.findall(text):
        label = f"{a}-{b}"
        try:
            start, end = to_seconds(a), to_seconds(b)
        except ValueError as e:
            errors.append(str(e))
            continue
        if end <= start:
            errors.append(f"«{label}»: конец должен быть позже начала")
        elif end - start < MIN_FRAGMENT_SEC:
            errors.append(f"«{label}»: слишком короткий фрагмент")
        elif end - start > MAX_FRAGMENT_SEC:
            errors.append(f"«{label}»: длиннее {MAX_FRAGMENT_SEC // 60} минут")
        elif duration is not None and start >= duration:
            errors.append(f"«{label}»: видео длится всего {fmt(duration)}")
        else:
            if duration is not None:
                end = min(end, duration)
            ranges.append((start, end))
    if len(ranges) > MAX_FRAGMENTS:
        errors.append(f"Можно не больше {MAX_FRAGMENTS} фрагментов за раз — взял первые {MAX_FRAGMENTS}")
        ranges = ranges[:MAX_FRAGMENTS]
    return ranges, errors


def fit_to_duration(ranges: list[tuple[float, float]],
                    duration: float) -> tuple[list[tuple[float, float]], list[str]]:
    """Отбрасывает отрезки за концом видео и подрезает те, что выходят за конец."""
    ok, errors = [], []
    for start, end in ranges:
        if start >= duration:
            errors.append(f"«{fmt(start)}-{fmt(end)}»: видео длится всего {fmt(duration)}")
        else:
            ok.append((start, min(end, duration)))
    return ok, errors
