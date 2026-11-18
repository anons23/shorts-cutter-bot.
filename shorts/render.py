"""Рендер шортса через FFmpeg: вырезка, вертикальный формат, уникализация, субтитры."""
import json
import os
import shutil
import subprocess

from . import config
from .face import face_center_x
from .subtitles import build_ass

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")

# Уровни уникализации. zoom — приближение, speed — ускорение, flip — зеркало.
UNIQUE_PRESETS = {
    "none": {"zoom": 1.0, "speed": 1.0, "flip": False, "eq": None, "noise": 0},
    "light": {"zoom": 1.04, "speed": 1.0, "flip": False,
              "eq": "contrast=1.03:saturation=1.08", "noise": 0},
    "strong": {"zoom": 1.08, "speed": 1.04, "flip": True,
               "eq": "contrast=1.06:saturation=1.15:brightness=0.02:gamma=0.98", "noise": 4},
}
LAYOUTS = ("blur", "crop")  # blur — видео целиком на размытом фоне; crop — обрезка (по лицу, если найдено)


def probe(video: str) -> dict:
    out = subprocess.run(
        [config.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", video],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise ValueError("В файле нет видеодорожки")
    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "duration": float(info["format"].get("duration") or v.get("duration") or 0),
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


def _video_filter(w: int, h: int, layout: str, p: dict, overlay: bool = True,
                  face_x: float | None = None) -> str:
    flip = "hflip," if p["flip"] else ""
    if layout == "crop":
        if face_x is None:
            pos = ""  # по центру
        else:
            c = 1 - face_x if p["flip"] else face_x  # после зеркала лицо тоже отражается
            pos = f":'min(max({c:.4f}*iw-{w}/2,0),iw-{w})':0"
        chain = f"[0:v]{flip}scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}{pos}"
    else:
        chain = (f"[0:v]{flip}split[a][b];"
                 f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bg];"
                 f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    if p["zoom"] > 1:
        chain += f",scale=trunc(iw*{p['zoom']}/2)*2:-2,crop={w}:{h}"
    if p["eq"]:
        chain += f",eq={p['eq']}"
    if p["noise"]:
        chain += f",noise=alls={p['noise']}:allf=t"
    if overlay:  # надписи после зеркала, чтобы текст не отразился
        chain += ",subtitles=subs.ass:fontsdir=fonts"
    if p["speed"] != 1:
        chain += f",setpts=PTS/{p['speed']}"
    return chain + ",format=yuv420p[v]"


def _copy_fonts(work_dir: str) -> None:
    """Шрифты кладём рядом с субтитрами: так путь к ним не ломается на Windows."""
    dst = os.path.join(work_dir, "fonts")
    if os.path.isdir(dst) or not os.path.isdir(FONTS_DIR):
        return
    os.makedirs(dst)
    for name in os.listdir(FONTS_DIR):
        if name.lower().endswith((".otf", ".ttf")):
            shutil.copy(os.path.join(FONTS_DIR, name), dst)


def render_clip(video: str, info: dict, words: list[dict], start: float, end: float,
                out_path: str, work_dir: str, unique: str = "light", layout: str = "blur",
                resolution: int = config.RESOLUTION, subtitles: bool = True,
                style: str = "classic", position: str = "bottom", hook: str = "",
                watermark: str = "", face: bool = True) -> str:
    w = resolution - resolution % 2
    h = round(w * 16 / 9 / 2) * 2
    p = UNIQUE_PRESETS.get(unique, UNIQUE_PRESETS["light"])
    layout = layout if layout in LAYOUTS else "blur"

    subtitles = subtitles and bool(words)
    overlay = subtitles or bool(hook.strip()) or bool(watermark.strip())
    if overlay:
        _copy_fonts(work_dir)
        with open(os.path.join(work_dir, "subs.ass"), "w", encoding="utf-8") as f:
            f.write(build_ass(words if subtitles else [], start, end, w, h, font=config.SUB_FONT,
                              style=style, position=position, hook=hook, watermark=watermark))
    face_x = face_center_x(video, start, end) if layout == "crop" and face else None

    cmd = [config.FFMPEG, "-y", "-loglevel", "error",
           "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", os.path.abspath(video),
           "-filter_complex", _video_filter(w, h, layout, p, overlay, face_x), "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        if p["speed"] != 1:
            cmd += ["-af", f"atempo={p['speed']}"]
    cmd += ["-c:v", config.VIDEO_ENCODER]
    if config.VIDEO_ENCODER == "libx264":
        cmd += ["-preset", "veryfast", "-crf", "23"]
    cmd += ["-movflags", "+faststart", os.path.abspath(out_path)]

    # запускаем из рабочей папки: так пути к subs.ass и шрифтам не ломаются на Windows
    res = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg упал: {res.stderr[-1500:]}")
    return out_path
