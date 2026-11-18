"""Поиск лица для вертикальной обрезки: чтобы говорящий не уходил за край кадра.

Нужна библиотека OpenCV (opencv-python-headless). Если её нет — обрезка по центру (заглушка).
"""
import os
import statistics

try:
    import cv2
except ImportError:  # заглушка: без OpenCV режем по центру
    cv2 = None

MAX_SAMPLES = 16


def _cascade_path() -> str | None:
    """Модель лица: своя (FACE_CASCADE в .env) или встроенная в OpenCV 4."""
    candidates = [os.getenv("FACE_CASCADE", "")]
    if cv2 is not None and hasattr(cv2, "data"):
        candidates.append(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    return next((c for c in candidates if c and os.path.exists(c)), None)


_detector_cache: list = []


def _detector():
    """Функция поиска лиц на сером кадре -> [(x, y, w, h)], или None, если искать нечем."""
    if _detector_cache:
        return _detector_cache[0]
    detect = None
    path = _cascade_path() if cv2 is not None else None
    if path:
        cascade = cv2.CascadeClassifier(path)
        if not cascade.empty():
            def detect(gray):
                return list(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                                     minSize=(38, 38)))
    if detect is None:  # запасной вариант: детектор из scikit-image, если он установлен
        try:
            from skimage import data as skdata
            from skimage.feature import Cascade
            sk = Cascade(skdata.lbp_frontal_face_cascade_filename())

            def detect(gray):
                found = sk.detect_multi_scale(img=gray, scale_factor=1.2, step_ratio=1,
                                              min_size=(38, 38), max_size=(400, 400))
                return [(f["c"], f["r"], f["width"], f["height"]) for f in found]
        except Exception:  # noqa: BLE001
            detect = None
    _detector_cache.append(detect)
    return detect


def available() -> bool:
    return cv2 is not None and _detector() is not None


def face_center_x(video: str, start: float, end: float) -> float | None:
    """Где по горизонтали чаще всего находится лицо: 0 — левый край, 1 — правый. None — не нашли."""
    detect = _detector() if cv2 is not None else None
    if detect is None:  # заглушка: нет OpenCV или модели — режем по центру
        return None
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return None
    centers = []
    try:
        duration = max(0.1, end - start)
        n = max(3, min(MAX_SAMPLES, int(duration / 1.5)))
        for k in range(n):
            t = start + duration * (k + 0.5) / n
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            scale = 480 / max(h, 1)  # уменьшаем кадр — так в разы быстрее
            small = cv2.resize(frame, (max(1, int(w * scale)), 480))
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            faces = detect(gray)
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])  # самое крупное лицо
                centers.append((x + fw / 2) / small.shape[1])
    finally:
        cap.release()
    if len(centers) < max(2, n // 4):  # лицо почти не видно — лучше по центру
        return None
    return float(statistics.median(centers))
