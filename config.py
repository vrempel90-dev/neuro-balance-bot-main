from functools import lru_cache
from typing import Any
from pydantic import Field, AliasChoices, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    crm_base_url: str = "https://neuro-balance-crm.vercel.app"
    crm_bot_secret: str = Field(validation_alias=AliasChoices("CRM_BOT_SECRET", "EXTERNAL_BOOKING_API_SECRET", "X_BOT_SECRET"))

    # ИИ используется экономно: только для классификации жалобы / нестандартного текста.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_voice_model: str = "whisper-1"
    ai_enabled: bool = True

    # Wazzup
    wazzup_api_key: str
    wazzup_channel_id: str
    wazzup_api_url: str = "https://api.wazzup24.com/v3"
    wazzup_media_endpoint_template: str = "{api_url}/media/{file_id}"

    webhook_secret: str = ""
    sqlite_path: str = "bot.sqlite3"

    # Сколько дней вперёд показывать при автоподборе окошек
    slot_search_days: int = 7
    max_slots_to_show: int = 5

    # Бюджет: цель — не выходить за 20 000 ₸/мес по ИИ.
    # В этой версии ИИ вызывается только для неочевидной классификации жалобы.
    monthly_ai_budget_kzt: int = 20000
    ai_max_classifier_calls_per_day: int = 300
    operator_style_mode: bool = True
    human_dialog_mode: bool = True

    # Режим работы: бот отвечает только вне рабочего времени КЦ.
    # По умолчанию 20:00–08:00 по времени Астаны (UTC+5). В Railway ставить BOT_ACTIVE_FROM=20 и BOT_ACTIVE_TO=8.
    work_hours_guard_enabled: bool = True
    bot_active_from_hour: int = Field(default=20, validation_alias=AliasChoices("BOT_ACTIVE_FROM", "BOT_ACTIVE_FROM_HOUR"))
    bot_active_until_hour: int = Field(default=8, validation_alias=AliasChoices("BOT_ACTIVE_TO", "BOT_ACTIVE_UNTIL_HOUR"))
    bot_silent_outside_hours: bool = True
    message_debounce_seconds: int = 5
    timezone_offset_hours: int = 5
    daytime_handoff_message: str = "Здравствуйте! Сейчас рабочее время контакт-центра, поэтому Ваше сообщение передано администратору. Он ответит Вам в порядке очереди🌿"



    @field_validator("bot_active_from_hour", "bot_active_until_hour", mode="before")
    @classmethod
    def parse_hour_value(cls, value: Any) -> int:
        """Railway может хранить время как 20, 20:00 или 20/00.
        Для логики бота нам нужен только час: 20 => 20:00, 8 => 08:00.
        """
        if value is None or value == "":
            return value
        if isinstance(value, int):
            if 0 <= value <= 23:
                return value
            raise ValueError("Hour must be from 0 to 23")
        text = str(value).strip().lower()
        text = text.replace(".", ":").replace("/", ":")
        hour_part = text.split(":", 1)[0]
        if not hour_part.isdigit():
            raise ValueError("Use hour format: 20, 8, 20:00 or 20/00")
        hour = int(hour_part)
        if not 0 <= hour <= 23:
            raise ValueError("Hour must be from 0 to 23")
        return hour

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
