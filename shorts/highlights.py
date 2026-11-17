"""Выбор интересных моментов: нейросеть читает транскрипт и предлагает отрезки."""
import json
import math
import re
import time

import requests

from . import config

MAX_CHARS_PER_REQUEST = 24000  # примерно 6-8 тыс. токенов на запрос, чтобы влезть в бесплатные лимиты

PROMPT = """Ты опытный монтажёр коротких вертикальных видео (TikTok, YouTube Shorts, Reels).
Ниже транскрипт длинного видео. Каждая строка: [начало-конец в секундах] текст.

Найди {n} самых интересных фрагментов для шортсов. Требования к каждому:
- длина от {min_s} до {max_s} секунд;
- фрагмент понятен без остального видео (законченная мысль, история, совет, шутка, спорное мнение);
- в первые 3 секунды есть зацепка (хук), которая не даст пролистать;
- фрагменты не пересекаются;
- start и end бери из таймкодов строк транскрипта.

Ответь ТОЛЬКО JSON без пояснений:
{{"clips": [{{"start": 12.5, "end": 58.0, "title": "Короткий цепляющий заголовок", "score": 8}}]}}
где score — оценка вирусности от 1 до 10.

Транскрипт:
{transcript}"""


def _format_lines(segments: list[dict]) -> list[str]:
    return [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments]


def _split_parts(lines: list[str]) -> list[str]:
    parts, cur, size = [], [], 0
    for line in lines:
        if size + len(line) > MAX_CHARS_PER_REQUEST and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def _ask_llm(transcript: str, n: int) -> list[dict]:
    if not config.LLM_API_KEY:
        raise RuntimeError("Не задан LLM_API_KEY (или GROQ_API_KEY) в файле .env")
    body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(
            n=n, min_s=config.CLIP_MIN_SEC, max_s=config.CLIP_MAX_SEC, transcript=transcript)}],
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }
    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    r = None
    for _ in range(4):
        r = requests.post(url, headers=headers, json=body, timeout=180)
        if r.status_code == 429:  # лимит запросов — ждём и пробуем снова
            time.sleep(min(float(r.headers.get("retry-after", 15)), 90))
            continue
        if r.status_code >= 500:
            time.sleep(5)
            continue
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")  # не все модели умеют JSON-режим
            continue
        break
    if r is None or r.status_code != 200:
        raise RuntimeError(f"Нейросеть вернула ошибку {r.status_code}: {r.text[:500]}")
    text = r.json()["choices"][0]["message"]["content"] or ""
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except json.JSONDecodeError:
        return []
    clips = data.get("clips", []) if isinstance(data, dict) else []
    return [c for c in clips if isinstance(c, dict)]


def _snap(clip: dict, segments: list[dict], duration: float) -> dict | None:
    """Подгоняет границы к началу/концу фраз и к допустимой длине."""
    try:
        start, end = float(clip["start"]), float(clip["end"])
    except (KeyError, TypeError, ValueError):
        return None
    starts = [s["start"] for s in segments if s["start"] <= start + 0.5]
    start = max(starts) if starts else start
    ends = [s["end"] for s in segments if s["end"] >= end - 0.5]
    end = min(ends) if ends else end
    if end - start > config.CLIP_MAX_SEC:
        # обрезаем по концу последней фразы, которая влезает в лимит
        fit = [s["end"] for s in segments if start < s["end"] <= start + config.CLIP_MAX_SEC]
        end = max(fit) if fit else start + config.CLIP_MAX_SEC
    start, end = max(0.0, start - 0.15), min(duration, end + 0.3)  # небольшой запас
    if end - start < config.CLIP_MIN_SEC * 0.7:
        return None
    return {"start": round(start, 2), "end": round(end, 2),
            "title": str(clip.get("title", "")).strip() or "Шортс",
            "score": clip.get("score", 0)}


def _remove_overlaps(clips: list[dict]) -> list[dict]:
    result = []
    for c in sorted(clips, key=lambda c: -float(c.get("score") or 0)):
        if all(c["end"] <= r["start"] or c["start"] >= r["end"] for r in result):
            result.append(c)
    return result


def fallback_clips(segments: list[dict], n: int, duration: float) -> list[dict]:
    """Без нейросети: берём участки с самой плотной речью."""
    target = (config.CLIP_MIN_SEC + config.CLIP_MAX_SEC) / 2
    candidates = []
    for i, s in enumerate(segments):
        j, words = i, 0
        while j < len(segments) and segments[j]["end"] - s["start"] <= target:
            words += len(segments[j]["text"].split())
            j += 1
        if j > i:
            end = segments[j - 1]["end"]
            if end - s["start"] >= config.CLIP_MIN_SEC:
                candidates.append({"start": s["start"], "end": end, "score": words / (end - s["start"]),
                                   "title": f"Момент {len(candidates) + 1}"})
    picked = _remove_overlaps(candidates)[:n]
    for k, c in enumerate(sorted(picked, key=lambda c: c["start"]), 1):
        c["title"] = f"Момент {k}"
    snapped = [_snap(c, segments, duration) for c in picked]
    return sorted((c for c in snapped if c), key=lambda c: c["start"])


def find_highlights(transcript: dict, n: int, duration: float, use_llm: bool = True) -> list[dict]:
    segments = transcript["segments"]
    if not segments:
        return []
    if not use_llm:
        return fallback_clips(segments, n, duration)
    parts = _split_parts(_format_lines(segments))
    per_part = max(2, math.ceil(n * 1.5 / len(parts)))  # просим с запасом, лишнее отсеем
    raw = []
    for part in parts:
        raw.extend(_ask_llm(part, per_part))
    clips = [c for c in (_snap(c, segments, duration) for c in raw) if c]
    clips = _remove_overlaps(clips)[:n]
    if not clips:
        return fallback_clips(segments, n, duration)
    return sorted(clips, key=lambda c: c["start"])
