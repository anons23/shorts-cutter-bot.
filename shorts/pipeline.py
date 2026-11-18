"""Весь процесс целиком: видео -> транскрипт -> моменты -> готовые шортсы."""
import json
import os
import re
import tempfile
from typing import Callable

from . import config
from .highlights import find_highlights
from .render import probe, render_clip
from .timecodes import fmt
from .transcribe import fake_transcript, fake_words, transcribe, transcribe_range


class ServiceNotReady(RuntimeError):
    """Нужный внешний сервис (ИИ и т.п.) ещё не настроен."""


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "_")[:40] or "clip"


def make_shorts(video: str, out_dir: str, n_clips: int = config.DEFAULT_CLIPS,
                transcript_path: str | None = None, fake: bool = False, use_llm: bool = True,
                unique: str = "light", layout: str = "blur", resolution: int = config.RESOLUTION,
                subtitles: bool = True,
                progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Автоматический режим. Возвращает список: [{"path", "title", "start", "end"}]."""
    if not (transcript_path or fake or config.speech_ready()):
        raise ServiceNotReady("распознавание речи ещё не подключено (нет GROQ_API_KEY)")
    use_llm = use_llm and config.llm_ready()  # без нейросети — простой выбор моментов
    os.makedirs(out_dir, exist_ok=True)
    info = probe(video)

    with tempfile.TemporaryDirectory(prefix="shorts_") as work:
        if transcript_path:
            with open(transcript_path, encoding="utf-8") as f:
                transcript = json.load(f)
        elif fake:
            transcript = fake_transcript(info["duration"])
        else:
            progress("🎙 Распознаю речь...")
            transcript = transcribe(video, work)
        with open(os.path.join(out_dir, "transcript.json"), "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, indent=1)

        if not transcript["segments"]:
            raise ValueError("В видео не найдено речи")

        progress("🧠 Ищу интересные моменты...")
        try:
            clips = find_highlights(transcript, n_clips, info["duration"], use_llm=use_llm)
        except Exception as e:  # noqa: BLE001 — нейросеть недоступна, но шортсы всё равно сделаем
            progress(f"⚠️ Нейросеть недоступна ({str(e)[:200]}). Выбираю моменты по простому правилу...")
            clips = find_highlights(transcript, n_clips, info["duration"], use_llm=False)
        if not clips:
            raise ValueError("Не нашёл подходящих моментов — видео слишком короткое?")

        results = []
        for i, c in enumerate(clips, 1):
            progress(f"🎬 Монтирую шортс {i} из {len(clips)}...")
            out = os.path.join(out_dir, f"{i:02d}_{_safe_name(c['title'])}.mp4")
            render_clip(video, info, transcript["words"], c["start"], c["end"], out, work,
                        unique=unique, layout=layout, resolution=resolution, subtitles=subtitles)
            results.append({"path": out, **_size(resolution), **c})
    return results


def _size(resolution: int) -> dict:
    w = resolution - resolution % 2
    return {"width": w, "height": round(w * 16 / 9 / 2) * 2}


def cut_fragments(fragments: list[dict], out_dir: str, subtitles: bool = True, fake: bool = False,
                  unique: str = "light", layout: str = "blur", resolution: int = config.RESOLUTION,
                  progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Ручной режим: режет ровно те отрезки, которые указал человек.

    fragments: [{"video": путь, "start": с, "end": по, "orig_start", "orig_end"}]
    orig_* — время в исходном видео (для подписи), если файл уже является вырезанным куском.
    Распознавание речи идёт только по этим отрезкам — это в разы дешевле авто-режима.
    """
    os.makedirs(out_dir, exist_ok=True)
    if subtitles and not fake and not config.speech_ready():
        subtitles = False  # заглушка: без ключа распознавания режем без субтитров
    results, subs_failed = [], False
    infos: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="shorts_") as work:
        for i, f in enumerate(fragments, 1):
            video, start, end = f["video"], f["start"], f["end"]
            info = infos.setdefault(video, probe(video))
            end = min(end, info["duration"]) if info["duration"] else end
            words = []
            if subtitles and not subs_failed and info["has_audio"]:
                progress(f"🎙 Распознаю речь во фрагменте {i} из {len(fragments)}...")
                try:
                    words = fake_words(start, end) if fake else transcribe_range(video, start, end, work)
                except Exception as e:  # noqa: BLE001 — без субтитров, но фрагменты всё равно отдадим
                    subs_failed = True
                    progress(f"⚠️ Не получилось распознать речь ({str(e)[:150]}). Режу без субтитров...")
            progress(f"✂️ Монтирую фрагмент {i} из {len(fragments)}...")
            o_start, o_end = f.get("orig_start", start), f.get("orig_end", end)
            out = os.path.join(out_dir, f"{i:02d}_{_safe_name((fmt(o_start) + '_' + fmt(o_end)).replace(':', '-'))}.mp4")
            render_clip(video, info, words, start, end, out, work, unique=unique, layout=layout,
                        resolution=resolution, subtitles=bool(words))
            results.append({"path": out, **_size(resolution), "title": f"Фрагмент {i}",
                            "start": o_start, "end": o_end, "subtitles": bool(words)})
    return results
