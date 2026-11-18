"""Весь процесс целиком: видео -> транскрипт -> моменты -> готовые шортсы."""
import json
import os
import re
import tempfile
from typing import Callable

from . import config
from .highlights import find_highlights, generate_hooks
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
                subtitles: bool = True, style: str = "classic", position: str = "bottom",
                hook: bool = False, watermark: str = "", face: bool = True,
                progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Автоматический режим. Возвращает список: [{"path", "title", "start", "end"}]."""
    if not (transcript_path or fake or config.speech_ready()):
        raise ServiceNotReady("распознавание речи ещё не подключено (нет GROQ_API_KEY)")
    use_llm = use_llm and config.llm_ready()  # без нейросети — простой выбор моментов
    look = {"style": style, "position": position, "watermark": watermark, "face": face}
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
            # заголовок от нейросети; у простого выбора («Момент 1») хука нет
            title = c["title"] if hook and use_llm and not c["title"].startswith("Момент") else ""
            render_clip(video, info, transcript["words"], c["start"], c["end"], out, work,
                        unique=unique, layout=layout, resolution=resolution, subtitles=subtitles,
                        hook=title, **look)
            results.append({"path": out, **_size(resolution), **c})
    return results


def _size(resolution: int) -> dict:
    w = resolution - resolution % 2
    return {"width": w, "height": round(w * 16 / 9 / 2) * 2}


def cut_fragments(fragments: list[dict], out_dir: str, subtitles: bool = True, fake: bool = False,
                  unique: str = "light", layout: str = "blur", resolution: int = config.RESOLUTION,
                  style: str = "classic", position: str = "bottom", hook: bool = False,
                  watermark: str = "", face: bool = True,
                  progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Ручной режим: режет ровно те отрезки, которые указал человек.

    fragments: [{"video": путь, "start": с, "end": по, "orig_start", "orig_end"}]
    orig_* — время в исходном видео (для подписи), если файл уже является вырезанным куском.
    Распознавание речи идёт только по этим отрезкам — это в разы дешевле авто-режима.
    """
    os.makedirs(out_dir, exist_ok=True)
    speech = fake or config.speech_ready()  # заглушка: без ключа распознавания — без субтитров и хуков
    need_words = speech and (subtitles or (hook and config.llm_ready()))
    results, failed = [], False
    infos: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="shorts_") as work:
        # 1) распознаём речь в каждом фрагменте (если нужны субтитры или хуки)
        prepared = []
        for i, f in enumerate(fragments, 1):
            video, start = f["video"], f["start"]
            info = infos.setdefault(video, probe(video))
            end = min(f["end"], info["duration"]) if info["duration"] else f["end"]
            words = []
            if need_words and not failed and info["has_audio"]:
                progress(f"🎙 Распознаю речь во фрагменте {i} из {len(fragments)}...")
                try:
                    words = fake_words(start, end) if fake else transcribe_range(video, start, end, work)
                except Exception as e:  # noqa: BLE001 — без субтитров, но фрагменты всё равно отдадим
                    failed = True
                    progress(f"⚠️ Не получилось распознать речь ({str(e)[:150]}). Режу без субтитров...")
            prepared.append((f, info, start, end, words))

        # 2) заголовки-хуки одним запросом к нейросети
        hooks = [""] * len(prepared)
        if hook and any(p[4] for p in prepared):
            progress("🧠 Придумываю заголовки...")
            hooks = generate_hooks([" ".join(w["word"] for w in p[4]) for p in prepared])

        # 3) монтаж
        for i, ((f, info, start, end, words), title) in enumerate(zip(prepared, hooks), 1):
            progress(f"✂️ Монтирую фрагмент {i} из {len(fragments)}...")
            o_start, o_end = f.get("orig_start", start), f.get("orig_end", end)
            name = _safe_name((fmt(o_start) + "_" + fmt(o_end)).replace(":", "-"))
            out = os.path.join(out_dir, f"{i:02d}_{name}.mp4")
            render_clip(f["video"], info, words, start, end, out, work, unique=unique, layout=layout,
                        resolution=resolution, subtitles=subtitles and bool(words), style=style,
                        position=position, hook=title, watermark=watermark, face=face)
            results.append({"path": out, **_size(resolution), "title": f"Фрагмент {i}", "hook": title,
                            "start": o_start, "end": o_end, "subtitles": subtitles and bool(words)})
    return results
