"""Настройки проекта. Все секреты берутся из файла .env (он не попадает в GitHub)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# --- Распознавание речи (Groq Whisper) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3")
LANGUAGE = os.getenv("LANGUAGE", "ru")

# --- Нейросеть для выбора моментов (любой OpenAI-совместимый API) ---
# По умолчанию тоже Groq, но можно указать DeepSeek, OpenRouter и т.д.
LLM_API_KEY = os.getenv("LLM_API_KEY") or GROQ_API_KEY
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

# --- Шортсы ---
CLIP_MIN_SEC = _int("CLIP_MIN_SEC", 20)
CLIP_MAX_SEC = _int("CLIP_MAX_SEC", 60)
DEFAULT_CLIPS = _int("DEFAULT_CLIPS", 5)
RESOLUTION = _int("RESOLUTION", 720)  # ширина вертикального видео: 720 -> 720x1280, 1080 -> 1080x1920

# --- Рендер ---
FFMPEG = os.getenv("FFMPEG", "ffmpeg")
FFPROBE = os.getenv("FFPROBE", "ffprobe")
VIDEO_ENCODER = os.getenv("VIDEO_ENCODER", "libx264")  # h264_nvenc для видеокарт NVIDIA
SUB_FONT = os.getenv("SUB_FONT", "Arial")


# --- Какие внешние сервисы подключены (без ключа включается заглушка) ---
def speech_ready() -> bool:
    """Распознавание речи (субтитры, авто-нарезка)."""
    return bool(GROQ_API_KEY)


def llm_ready() -> bool:
    """Нейросеть для выбора моментов. Без неё — простой выбор по плотности речи."""
    return bool(LLM_API_KEY)
