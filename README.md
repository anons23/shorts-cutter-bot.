# Shorts Cutter Bot

Telegram-бот, который превращает длинное видео в вертикальные шортсы:
находит интересные моменты, режет их в формат 9:16, слегка уникализирует и добавляет субтитры в стиле TikTok.

## Как это работает

1. **Распознавание речи** — Groq Whisper (бесплатный лимит), таймкод для каждого слова.
2. **Выбор моментов** — нейросеть читает транскрипт и выбирает фрагменты с хуком.
3. **Монтаж** — FFmpeg: вертикальный формат (размытый фон или обрезка), уникализация, субтитры с подсветкой слова.

```
shorts/            ядро (работает без Telegram)
  transcribe.py    распознавание речи
  highlights.py    выбор моментов
  subtitles.py     субтитры
  render.py        монтаж
  pipeline.py      всё вместе
clip.py            запуск из командной строки
bot.py             Telegram-бот
check.py           проверка настройки
tests/             тесты ядра (запускаются на GitHub при каждом изменении)
```

## Установка на Windows

1. **Python 3.11+** — скачай с python.org. При установке поставь галочку **Add Python to PATH**.
2. **FFmpeg** — в PowerShell: `winget install Gyan.FFmpeg`, потом перезапусти PowerShell.
3. **Git** — `winget install Git.Git`.
4. Скачай проект и установи зависимости:
   ```powershell
   git clone https://github.com/anons23/shorts-cutter-bot..git
   cd shorts-cutter-bot.
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
5. Скопируй `.env.example` в `.env` и впиши ключи: `copy .env.example .env`, затем `notepad .env`.

> Groq из России работает только через VPN — включи его перед запуском.

6. Проверь, что всё настроено: `python check.py`. Скрипт покажет, чего не хватает и как это исправить.

## Проверка

```powershell
# 1) Тест монтажа без интернета и ключей (фейковые субтитры):
python clip.py C:\путь\к\видео.mp4 --fake --no-llm --clips 2

# 2) Полный цикл с распознаванием речи и нейросетью:
python clip.py C:\путь\к\видео.mp4

# 3) Повторный монтаж без новых запросов к Groq:
python clip.py C:\путь\к\видео.mp4 --transcript output\transcript.json
```

Готовые шортсы появятся в папке `output`. Опции: `--unique none|light|strong`, `--layout blur|crop`, `--res 720|1080`, `--clips N`.

## Запуск бота

```powershell
python bot.py
```

В боте: пришли видео файлом (до 20 МБ без своего Bot API сервера) или ссылку на YouTube.
`/settings` — настройки, `/limit` — остаток на сегодня.

Для файлов до 2 ГБ впиши в `.env` адрес своего Local Bot API Server: `BOT_API_URL=http://localhost:8081`.

## Что дальше

- [ ] Кроп по лицу говорящего (MediaPipe)
- [ ] Проверить работу с Local Bot API Server на файлах до 2 ГБ
- [ ] Деплой на сервер
