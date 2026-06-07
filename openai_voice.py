from openai import AsyncOpenAI
from config import get_settings


async def transcribe_voice_bytes(data: bytes, filename: str = "voice.ogg", language: str = "ru") -> str:
    """Расшифровка голосового сообщения через OpenAI Whisper.

    Поддерживает .ogg / .mp3 / .wav / .m4a, если Wazzup отдаёт файл в bytes.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.audio.transcriptions.create(
        model=settings.openai_voice_model,
        file=(filename, data),
        language=language,
    )
    return result.text.strip()
