"""Проверка настройки: python check.py

Показывает, что установлено и что нет. Ключи на экран не выводятся.
"""
import os
import shutil
import subprocess
import sys

OK, BAD, WARN = "✅", "❌", "⚠️ "
problems = 0


def report(ok: bool, text: str, hint: str = "", warn: bool = False) -> None:
    global problems
    if ok:
        print(f"{OK} {text}")
        return
    print(f"{WARN if warn else BAD} {text}" + (f"\n     → {hint}" if hint else ""))
    if not warn:
        problems += 1


def main() -> None:
    report(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}",
           "Нужен Python 3.10 или новее: python.org")

    missing = []
    for mod, pip_name in [("requests", "requests"), ("dotenv", "python-dotenv"),
                          ("aiogram", "aiogram"), ("yt_dlp", "yt-dlp")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    report(not missing, "Библиотеки Python установлены",
           f"Не хватает: {', '.join(missing)}. Выполни: pip install -r requirements.txt")
    if "requests" in missing or "python-dotenv" in missing:
        print("\nСначала установи библиотеки, потом запусти проверку ещё раз.")
        sys.exit(1)

    import requests

    from shorts import config

    ffmpeg = shutil.which(config.FFMPEG)
    report(bool(ffmpeg), "FFmpeg найден", "Установи: winget install Gyan.FFmpeg и перезапусти терминал")
    report(bool(shutil.which(config.FFPROBE)), "FFprobe найден", "Он ставится вместе с FFmpeg")
    if ffmpeg:
        filters = subprocess.run([config.FFMPEG, "-hide_banner", "-filters"],
                                 capture_output=True, text=True).stdout
        report(" subtitles " in filters, "FFmpeg умеет вшивать субтитры (libass)",
               "Нужна полная сборка FFmpeg (Gyan.FFmpeg — full build)")
        if config.VIDEO_ENCODER != "libx264":
            encoders = subprocess.run([config.FFMPEG, "-hide_banner", "-encoders"],
                                      capture_output=True, text=True).stdout
            report(config.VIDEO_ENCODER in encoders, f"Кодировщик {config.VIDEO_ENCODER} доступен",
                   "Поставь VIDEO_ENCODER=libx264 в .env")

    report(os.path.exists(".env"), "Файл .env есть", "Выполни: copy .env.example .env и впиши ключи")

    # --- Groq: распознавание речи ---
    if not config.GROQ_API_KEY:
        report(False, "GROQ_API_KEY задан", "Создай ключ на console.groq.com и впиши в .env")
    else:
        models = _models("https://api.groq.com/openai/v1", config.GROQ_API_KEY, requests, "Groq")
        if models is not None:
            report(config.WHISPER_MODEL in models, f"Модель распознавания {config.WHISPER_MODEL} доступна",
                   "Впиши в .env: WHISPER_MODEL=whisper-large-v3-turbo")

    # --- Нейросеть для выбора моментов ---
    if config.LLM_API_KEY:
        models = _models(config.LLM_BASE_URL.rstrip("/"), config.LLM_API_KEY, requests, "Нейросеть")
        if models is not None:
            chat = sorted(m for m in models if "whisper" not in m and "guard" not in m and "tts" not in m)
            report(config.LLM_MODEL in models, f"Модель для выбора моментов {config.LLM_MODEL} доступна",
                   "Впиши в .env LLM_MODEL= одну из доступных: " + ", ".join(chat[:12]))

    # --- Telegram ---
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        report(False, "BOT_TOKEN задан", "Получи токен у @BotFather и впиши в .env")
    else:
        base = (os.getenv("BOT_API_URL") or "https://api.telegram.org").rstrip("/")
        where = "локальный Bot API" if os.getenv("BOT_API_URL") else "Telegram"
        try:
            r = requests.get(f"{base}/bot{token}/getMe", timeout=15)
            name = r.json().get("result", {}).get("username", "")
            report(r.status_code == 200, f"Бот отвечает через {where}: @{name}",
                   "Токен неверный — проверь BOT_TOKEN в .env")
        except requests.RequestException as e:
            report(False, f"Связь с ботом через {where}",
                   f"Нет соединения ({type(e).__name__}). Проверь VPN и BOT_API_URL")
    if not os.getenv("PAYMENT_PROVIDER"):
        report(False, "Оплата не подключена — кнопка «Купить» работает как заглушка", warn=True)
    webapp = os.getenv("WEBAPP_URL", "")
    if not webapp:
        report(False, "WEBAPP_URL не задан — окна разметки не будет, только ввод в чате", warn=True)
    elif not webapp.startswith("https://"):
        report(False, "WEBAPP_URL начинается с https://", "Telegram открывает окна только по https")
    else:
        try:
            ok = requests.get(webapp, timeout=15).status_code == 200
        except requests.RequestException:
            ok = False
        report(ok, "Окно разметки открывается", "Проверь, что GitHub Pages включены и адрес верный")
    if not os.getenv("BOT_API_URL"):
        report(False, "BOT_API_URL не задан — бот примет файлы только до 20 МБ", warn=True)

    print("\n" + ("Всё готово! Запускай: python bot.py" if problems == 0
                  else f"Нужно исправить пунктов: {problems}"))
    sys.exit(1 if problems else 0)


def _models(base: str, key: str, requests, name: str) -> set | None:
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException as e:
        report(False, f"{name}: связь с сервисом",
               f"Нет соединения ({type(e).__name__}). Из России нужен VPN")
        return None
    if r.status_code in (401, 403):
        report(False, f"{name}: ключ принят",
               "Ключ неверный, или сервис недоступен из твоей страны (включи VPN)")
        return None
    if r.status_code != 200:
        report(False, f"{name}: список моделей получен", f"Код ответа {r.status_code}", warn=True)
        return None
    report(True, f"{name}: ключ работает")
    return {m.get("id", "") for m in r.json().get("data", [])}


if __name__ == "__main__":
    main()
