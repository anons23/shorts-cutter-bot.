"""Запуск без Telegram.

Примеры:
  python clip.py video.mp4                      # полный цикл (нужны ключи в .env)
  python clip.py video.mp4 --fake --no-llm      # тест без интернета и ключей
  python clip.py video.mp4 --unique strong --layout crop --clips 3
"""
import argparse
import sys

from shorts import config
from shorts.pipeline import make_shorts


def main() -> None:
    ap = argparse.ArgumentParser(description="Нарезка длинного видео на шортсы")
    ap.add_argument("video", help="путь к видео")
    ap.add_argument("--out", default="output", help="папка для результата (по умолчанию output)")
    ap.add_argument("--clips", type=int, default=config.DEFAULT_CLIPS, help="сколько шортсов сделать")
    ap.add_argument("--unique", choices=["none", "light", "strong"], default="light", help="уникализация")
    ap.add_argument("--layout", choices=["blur", "crop"], default="blur",
                    help="blur — на размытом фоне, crop — обрезка по центру")
    ap.add_argument("--res", type=int, default=config.RESOLUTION, help="ширина: 720 или 1080")
    ap.add_argument("--transcript", help="готовый transcript.json (не тратить запросы к Groq)")
    ap.add_argument("--fake", action="store_true", help="фейковый транскрипт для теста")
    ap.add_argument("--no-llm", action="store_true", help="выбирать моменты без нейросети")
    a = ap.parse_args()

    try:
        results = make_shorts(a.video, a.out, n_clips=a.clips, transcript_path=a.transcript,
                              fake=a.fake, use_llm=not a.no_llm, unique=a.unique,
                              layout=a.layout, resolution=a.res, progress=print)
    except Exception as e:  # noqa: BLE001
        print(f"❌ Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"\n✅ Готово, шортсов: {len(results)}")
    for r in results:
        print(f"  {r['start']:7.1f}–{r['end']:7.1f} c  {r['title']}  ->  {r['path']}")


if __name__ == "__main__":
    main()
