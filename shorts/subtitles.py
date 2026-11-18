"""Надписи на шортсе в формате ASS: субтитры по словам, заголовок-хук и водяной знак.

Цвета в ASS пишутся как &HAABBGGRR& (сначала прозрачность, потом синий, зелёный, красный).
"""

# Готовые стили субтитров. Шрифты лежат в папке fonts/ проекта.
STYLES = {
    "classic": {"title": "Классика", "font": "Inter ExtraBold", "size": 0.052, "upper": True,
                "color": "&H00FFFFFF&", "hl": "&H0000E5FF&", "outline": "&H00000000&",
                "outline_w": 0.09, "shadow": 0.5, "box": False, "blur": 0},
    "box": {"title": "Плашка", "font": "Inter ExtraBold", "size": 0.046, "upper": False,
            "color": "&H00FFFFFF&", "hl": "&H0000E5FF&", "outline": "&H64000000&",
            "outline_w": 0.28, "shadow": 0, "box": True, "blur": 0},
    "neon": {"title": "Неон", "font": "Inter Black", "size": 0.052, "upper": True,
             "color": "&H00FFFFFF&", "hl": "&H00FFF200&", "outline": "&H00B000FF&",
             "outline_w": 0.10, "shadow": 0, "box": False, "blur": 3},
    "fire": {"title": "Огонь", "font": "Inter Black", "size": 0.050, "upper": True,
             "color": "&H00FFFFFF&", "hl": "&H00004DFF&", "outline": "&H00000000&",
             "outline_w": 0.12, "shadow": 0.8, "box": False, "blur": 0},
    "minimal": {"title": "Минимал", "font": "Inter SemiBold", "size": 0.044, "upper": False,
                "color": "&H00FFFFFF&", "hl": "&H00FFFFFF&", "outline": "&H00000000&",
                "outline_w": 0.05, "shadow": 0.6, "box": False, "blur": 0},
}
POSITIONS = {"bottom": "Внизу", "middle": "По центру"}
HOOK_SEC = 3.0  # сколько секунд висит заголовок-хук


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", " ").strip()


def _group(words: list[dict], max_words: int = 3, max_chars: int = 15,
           max_gap: float = 0.6) -> list[list[dict]]:
    """Группирует слова так, чтобы строка помещалась в одну линию."""
    groups, cur = [], []
    for w in words:
        too_long = sum(len(x["word"]) + 1 for x in cur) + len(w["word"]) > max_chars
        if cur and (len(cur) >= max_words or too_long or w["start"] - cur[-1]["end"] > max_gap):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["word"][-1:] in ".!?,:;…":  # конец фразы — новая группа
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], start: float, end: float, width: int, height: int,
              font: str | None = None, uppercase: bool | None = None, style: str = "classic",
              position: str = "bottom", hook: str = "", watermark: str = "") -> str:
    """Файл .ass для отрезка [start, end] исходного видео.

    words — слова с таймкодами (пусто — без субтитров); hook — заголовок сверху на первые секунды;
    watermark — маленькая надпись в углу на всё видео.
    """
    st = STYLES.get(style, STYLES["classic"])
    font = font if font and style == "classic" and font != "Arial" else st["font"]
    upper = st["upper"] if uppercase is None else uppercase
    size = round(height * st["size"])
    outline = max(1, round(size * st["outline_w"]))
    shadow = round(size * st["shadow"] / 10)
    border_style = 3 if st["box"] else 1
    if position == "middle":
        align, margin_v = 5, 0
    else:
        align, margin_v = 2, round(height * 0.22)
    side = round(width * 0.08)

    hook_size = round(height * 0.050)
    wm_size = max(12, round(height * 0.020))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{st['color']},{st['color']},{st['outline']},&H80000000&,0,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{align},{side},{side},{margin_v},1
Style: Hook,Inter Black,{hook_size},&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,3,{round(hook_size * 0.32)},0,8,{side},{side},{round(height * 0.11)},1
Style: Mark,Inter SemiBold,{wm_size},&H66FFFFFF&,&H66FFFFFF&,&H99000000&,&H00000000&,0,0,0,0,100,100,0,0,1,1,0,9,{round(width * 0.04)},{round(width * 0.04)},{round(height * 0.03)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    duration = end - start
    lines = []
    blur = f"\\blur{st['blur']}" if st["blur"] else ""
    clip_words = [w for w in words if w["end"] > start and w["start"] < end and w["word"].strip()]
    for group in _group(clip_words):
        texts = [_clean(w["word"]) for w in group]
        if upper:
            texts = [t.upper() for t in texts]
        for i, w in enumerate(group):
            t0 = w["start"] - start
            t1 = (group[i + 1]["start"] if i + 1 < len(group) else w["end"] + 0.15) - start
            t1 = min(t1, duration)
            if t1 <= t0:
                continue
            parts = [f"{{\\c{st['hl']}}}{t}{{\\c{st['color']}}}" if k == i and st["hl"] != st["color"] else t
                     for k, t in enumerate(texts)]
            pop = "\\fscx108\\fscy108\\t(0,80,\\fscx100\\fscy100)" if i == 0 else ""
            tags = f"{{{blur}{pop}}}" if blur or pop else ""
            lines.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Main,,0,0,0,,{tags}{' '.join(parts)}")

    if hook.strip():
        text = _clean(hook)[:80].upper()
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(HOOK_SEC, duration))},Hook,,0,0,0,,"
                     f"{{\\fad(120,300)}}{text}")
    if watermark.strip():
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Mark,,0,0,0,,{_clean(watermark)}")
    return header + "\n".join(lines) + "\n"
