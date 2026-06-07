from functools import lru_cache
from typing import Any

from pydantic import Field, AliasChoices, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    crm_base_url: str = Field(default="https://neuro-balance-crm.vercel.app", validation_alias=AliasChoices("CRM_BASE_URL"))
    crm_bot_secret: str = Field(default="", validation_alias=AliasChoices("CRM_BOT_SECRET", "BOT_API_SECRET"))

    openai_api_key: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_model: str = Field(default="gpt-4o-mini", validation_alias=AliasChoices("OPENAI_MODEL"))
    openai_voice_model: str = Field(default="whisper-1", validation_alias=AliasChoices("OPENAI_VOICE_MODEL"))
    ai_enabled: bool = Field(default=True, validation_alias=AliasChoices("AI_ENABLED"))
    openai_dialog_temperature: float = Field(default=0.25, validation_alias=AliasChoices("OPENAI_DIALOG_TEMPERATURE"))
    openai_humanize_temperature: float = Field(default=0.25, validation_alias=AliasChoices("OPENAI_HUMANIZE_TEMPERATURE"))
    openai_max_tokens: int = Field(default=1000, validation_alias=AliasChoices("OPENAI_MAX_TOKENS"))

    wazzup_api_key: str = Field(default="", validation_alias=AliasChoices("WAZZUP_API_KEY"))
    wazzup_channel_id: str = Field(default="", validation_alias=AliasChoices("WAZZUP_CHANNEL_ID"))
    wazzup_instagram_channel_id: str = Field(default="", validation_alias=AliasChoices("WAZZUP_INSTAGRAM_CHANNEL_ID"))
    wazzup_api_url: str = Field(default="https://api.wazzup24.com/v3", validation_alias=AliasChoices("WAZZUP_API_URL"))
    wazzup_media_endpoint_template: str = Field(default="{api_url}/media/{file_id}", validation_alias=AliasChoices("WAZZUP_MEDIA_ENDPOINT_TEMPLATE"))

    webhook_secret: str = Field(default="", validation_alias=AliasChoices("WEBHOOK_SECRET"))
    sqlite_path: str = Field(default="bot.sqlite3", validation_alias=AliasChoices("SQLITE_PATH"))

    slot_search_days: int = Field(default=7, validation_alias=AliasChoices("SLOT_SEARCH_DAYS"))
    max_slots_to_show: int = Field(default=5, validation_alias=AliasChoices("MAX_SLOTS_TO_SHOW"))

    monthly_ai_budget_kzt: int = Field(default=20000, validation_alias=AliasChoices("MONTHLY_AI_BUDGET_KZT"))
    ai_max_classifier_calls_per_day: int = Field(default=300, validation_alias=AliasChoices("AI_MAX_CLASSIFIER_CALLS_PER_DAY"))
    operator_style_mode: bool = Field(default=True, validation_alias=AliasChoices("OPERATOR_STYLE_MODE"))
    human_dialog_mode: bool = Field(default=True, validation_alias=AliasChoices("HUMAN_DIALOG_MODE"))

    work_hours_guard_enabled: bool = Field(default=True, validation_alias=AliasChoices("WORK_HOURS_GUARD_ENABLED"))
    bot_active_from_hour: int = Field(default=20, validation_alias=AliasChoices("BOT_ACTIVE_FROM", "BOT_ACTIVE_FROM_HOUR"))
    bot_active_until_hour: int = Field(default=8, validation_alias=AliasChoices("BOT_ACTIVE_TO", "BOT_ACTIVE_UNTIL_HOUR"))
    bot_silent_outside_hours: bool = Field(default=True, validation_alias=AliasChoices("BOT_SILENT_OUTSIDE_HOURS"))
    message_debounce_seconds: int = Field(default=5, validation_alias=AliasChoices("MESSAGE_DEBOUNCE_SECONDS"))
    timezone_offset_hours: int = Field(default=5, validation_alias=AliasChoices("TIMEZONE_OFFSET_HOURS"))
    daytime_handoff_message: str = Field(
        default="Здравствуйте! Сейчас рабочее время контакт-центра, поэтому Ваше сообщение передано администратору. Он ответит Вам в порядке очереди🌿",
        validation_alias=AliasChoices("DAYTIME_HANDOFF_MESSAGE"),
    )

    learn_admin_dialogs_enabled: bool = Field(default=True, validation_alias=AliasChoices("LEARN_ADMIN_DIALOGS_ENABLED"))
    admin_style_examples_limit: int = Field(default=18, validation_alias=AliasChoices("ADMIN_STYLE_EXAMPLES_LIMIT"))

    @field_validator("bot_active_from_hour", "bot_active_until_hour", mode="before")
    @classmethod
    def parse_hour_value(cls, value: Any) -> int:
        if value is None or value == "":
            return 0
        if isinstance(value, int):
            if 0 <= value <= 23:
                return value
            raise ValueError("Hour must be from 0 to 23")

        text = str(value).strip().lower().replace(".", ":").replace("/", ":")
        hour_part = text.split(":", 1)[0]
        if not hour_part.isdigit():
            raise ValueError("Use hour format: 20, 8, 20:00 or 20/00")

        hour = int(hour_part)
        if not 0 <= hour <= 23:
            raise ValueError("Hour must be from 0 to 23")
        return hour

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
