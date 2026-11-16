"""Весь процесс целиком: видео -> транскрипт -> моменты -> готовые шортсы."""
import json
import os
import re
import tempfile
from typing import Callable

from . import config
from .highlights import find_highlights
from .render import probe, render_clip
from .transcribe import fake_transcript, transcribe


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "_")[:40] or "clip"


def make_shorts(video: str, out_dir: str, n_clips: int = config.DEFAULT_CLIPS,
                transcript_path: str | None = None, fake: bool = False, use_llm: bool = True,
                unique: str = "light", layout: str = "blur", resolution: int = config.RESOLUTION,
                progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Возвращает список: [{"path", "title", "start", "end"}]."""
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
        clips = find_highlights(transcript, n_clips, info["duration"], use_llm=use_llm)
        if not clips:
            raise ValueError("Не нашёл подходящих моментов — видео слишком короткое?")

        results = []
        for i, c in enumerate(clips, 1):
            progress(f"🎬 Монтирую шортс {i} из {len(clips)}...")
            out = os.path.join(out_dir, f"{i:02d}_{_safe_name(c['title'])}.mp4")
            render_clip(video, info, transcript["words"], c["start"], c["end"], out, work,
                        unique=unique, layout=layout, resolution=resolution)
            results.append({"path": out, **c})
    return results
