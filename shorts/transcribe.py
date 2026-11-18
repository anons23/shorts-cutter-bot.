"""Распознавание речи через Groq Whisper API с таймкодами каждого слова."""
import glob
import os
import subprocess
import time

import requests

from . import config

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CHUNK_SEC = 1200  # режем звук на куски по 20 минут, чтобы влезть в лимит размера файла


def extract_audio(video: str, work_dir: str) -> list[str]:
    """Достаёт звук (моно, 16 кГц, mp3 32 кбит/с) и режет на куски."""
    pattern = os.path.join(work_dir, "audio_%03d.mp3")
    cmd = [
        config.FFMPEG, "-y", "-loglevel", "error", "-i", video,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(CHUNK_SEC), "-reset_timestamps", "1",
        pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(glob.glob(os.path.join(work_dir, "audio_*.mp3")))


def _duration(path: str) -> float:
    out = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return float(CHUNK_SEC)


def _transcribe_chunk(path: str) -> dict:
    if not config.GROQ_API_KEY:
        raise RuntimeError("Не задан GROQ_API_KEY в файле .env")
    data = [
        ("model", config.WHISPER_MODEL),
        ("language", config.LANGUAGE),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("timestamp_granularities[]", "segment"),
    ]
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    for attempt in range(5):
        with open(path, "rb") as f:
            r = requests.post(GROQ_URL, headers=headers, data=data,
                              files={"file": (os.path.basename(path), f)}, timeout=600)
        if r.status_code == 429:  # упёрлись в лимит бесплатного тарифа — ждём
            wait = float(r.headers.get("retry-after", 20))
            time.sleep(min(wait, 120))
            continue
        if r.status_code >= 500:  # временный сбой на стороне Groq
            time.sleep(5)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Groq вернул ошибку {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("Groq: слишком много запросов, попробуйте позже")


def transcribe(video: str, work_dir: str) -> dict:
    """Возвращает {"segments": [{start, end, text}], "words": [{word, start, end}]}."""
    segments, words = [], []
    offset = 0.0  # куски получаются не ровно по 20 минут — считаем по реальной длине
    for chunk in extract_audio(video, work_dir):
        res = _transcribe_chunk(chunk)
        for s in res.get("segments", []):
            segments.append({"start": s["start"] + offset, "end": s["end"] + offset,
                             "text": s["text"].strip()})
        for w in res.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"] + offset,
                          "end": w["end"] + offset})
        offset += _duration(chunk)
    return {"segments": segments, "words": words}


def transcribe_range(video: str, start: float, end: float, work_dir: str) -> list[dict]:
    """Распознаёт речь только в отрезке [start, end]. Возвращает слова с таймкодами исходного видео.

    Для ручной нарезки: отправляем в Groq пару минут звука вместо всего видео.
    """
    path = os.path.join(work_dir, f"range_{start:.2f}.mp3")
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.2f}",
                    "-t", f"{end - start:.2f}", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "libmp3lame", "-b:a", "32k", path], check=True)
    res = _transcribe_chunk(path)
    os.remove(path)
    return [{"word": w["word"].strip(), "start": w["start"] + start, "end": w["end"] + start}
            for w in res.get("words", []) if w["word"].strip()]


def fake_words(start: float, end: float) -> list[dict]:
    """Фейковые слова на отрезке — для тестов без интернета."""
    return [{**w, "start": w["start"] + start, "end": w["end"] + start}
            for w in fake_transcript(end - start)["words"]]


_FAKE_TEXT = (
    "Сегодня я расскажу вам одну историю которая изменила мою жизнь. "
    "Всё началось с того что я потерял работу и не знал что делать дальше. "
    "И тогда я решил попробовать то о чём давно мечтал. "
    "Самое главное не бояться ошибок потому что именно они нас учат. "
    "Вот три совета которые помогут вам начать прямо сегодня."
).split()


def fake_transcript(duration: float) -> dict:
    """Фейковый транскрипт для тестов без интернета и ключей."""
    words, segments = [], []
    t, i = 0.5, 0
    seg_start, seg_words = t, []
    while t + 0.4 < duration:
        w = _FAKE_TEXT[i % len(_FAKE_TEXT)]
        words.append({"word": w, "start": round(t, 2), "end": round(t + 0.35, 2)})
        seg_words.append(w)
        i += 1
        t += 0.42
        if w.endswith(".") or len(seg_words) >= 14:
            segments.append({"start": round(seg_start, 2), "end": round(t - 0.07, 2),
                             "text": " ".join(seg_words)})
            t += 0.3
            seg_start, seg_words = t, []
    if seg_words:
        segments.append({"start": round(seg_start, 2), "end": round(t, 2), "text": " ".join(seg_words)})
    return {"segments": segments, "words": words}
