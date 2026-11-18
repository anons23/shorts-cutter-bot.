"""Раскадровка: сетка кадров с таймкодами, чтобы удобно выбрать тайминги для загруженного видео."""
import os
import subprocess
import tempfile

from . import config
from .timecodes import fmt

try:
    import cv2
    import numpy as np
except ImportError:  # заглушка: без OpenCV раскадровки нет
    cv2 = None

COLS, ROWS = 3, 4
THUMB_W = 360


def available() -> bool:
    return cv2 is not None


def make_storyboard(video: str, duration: float, out_path: str,
                    cols: int = COLS, rows: int = ROWS) -> list[float]:
    """Сохраняет картинку-сетку и возвращает таймкоды кадров."""
    if cv2 is None:
        raise RuntimeError("для раскадровки нужна библиотека OpenCV")
    n = cols * rows
    times = [duration * (k + 0.5) / n for k in range(n)]
    tiles = []
    with tempfile.TemporaryDirectory(prefix="board_") as work:
        for k, t in enumerate(times):
            path = os.path.join(work, f"{k:02d}.jpg")
            subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video,
                            "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", "-q:v", "4", path],
                           check=False)
            img = cv2.imread(path)
            tiles.append(img)
    shape = next((t.shape for t in tiles if t is not None), (THUMB_W * 9 // 16, THUMB_W, 3))
    h, w = shape[:2]
    gap = 6
    board = np.full((rows * (h + gap) + gap, cols * (w + gap) + gap, 3), 24, np.uint8)
    for k, (img, t) in enumerate(zip(tiles, times)):
        if img is None:
            img = np.zeros(shape, np.uint8)
        img = cv2.resize(img, (w, h))
        label = fmt(t)
        scale, thick = h / 200, max(1, round(h / 90))
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        pad = round(h * 0.04)
        cv2.rectangle(img, (pad, pad), (pad * 3 + tw, pad * 3 + th), (0, 0, 0), -1)
        cv2.putText(img, label, (pad * 2, pad * 2 + th), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), thick, cv2.LINE_AA)
        r, c = divmod(k, cols)
        y, x = gap + r * (h + gap), gap + c * (w + gap)
        board[y:y + h, x:x + w] = img
    cv2.imwrite(out_path, board, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return times
