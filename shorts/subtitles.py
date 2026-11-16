"""Субтитры в стиле TikTok: по 1-3 слова, текущее слово подсвечено."""

HIGHLIGHT = "&H0000E5FF&"  # жёлтый (в ASS цвета пишутся как BGR)


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(word: str) -> str:
    return word.replace("{", "(").replace("}", ")").replace("\\", "/").strip()


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
              font: str = "Arial", uppercase: bool = True) -> str:
    """Делает файл субтитров .ass для отрезка [start, end] исходного видео."""
    size = round(height * 0.052)
    outline = max(2, round(size * 0.09))
    margin_v = round(height * 0.24)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{outline},{max(1, outline // 2)},2,{round(width * 0.08)},{round(width * 0.08)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clip_words = [w for w in words if w["end"] > start and w["start"] < end and w["word"].strip()]
    lines = []
    for group in _group(clip_words):
        texts = [_clean(w["word"]) for w in group]
        if uppercase:
            texts = [t.upper() for t in texts]
        for i, w in enumerate(group):
            t0 = w["start"] - start
            t1 = (group[i + 1]["start"] if i + 1 < len(group) else w["end"] + 0.15) - start
            t1 = min(t1, end - start)
            if t1 <= t0:
                continue
            parts = [f"{{\\c{HIGHLIGHT}}}{t}{{\\c&H00FFFFFF&}}" if k == i else t
                     for k, t in enumerate(texts)]
            pop = "{\\fscx108\\fscy108\\t(0,80,\\fscx100\\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Main,,0,0,0,,{pop}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"
