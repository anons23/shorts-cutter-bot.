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
        if r.status_code != 200:
            raise RuntimeError(f"Groq вернул ошибку {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("Groq: слишком много запросов, попробуйте позже")


def transcribe(video: str, work_dir: str) -> dict:
    """Возвращает {"segments": [{start, end, text}], "words": [{word, start, end}]}."""
    segments, words = [], []
    for i, chunk in enumerate(extract_audio(video, work_dir)):
        offset = i * CHUNK_SEC
        res = _transcribe_chunk(chunk)
        for s in res.get("segments", []):
            segments.append({"start": s["start"] + offset, "end": s["end"] + offset,
                             "text": s["text"].strip()})
        for w in res.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"] + offset,
                          "end": w["end"] + offset})
    return {"segments": segments, "words": words}


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
