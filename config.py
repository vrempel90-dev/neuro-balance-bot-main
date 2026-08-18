from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # CRM
    crm_base_url: str = Field(
        default="https://neuro-balance-crm.vercel.app",
        validation_alias=AliasChoices("CRM_BASE_URL", "crm_base_url"),
    )
    crm_bot_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CRM_BOT_SECRET",
            "EXTERNAL_BOOKING_API_SECRET",
            "X_BOT_SECRET",
        ),
    )

    # OpenAI / AI
    openai_api_key: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"))
    openai_model: str = Field(default="gpt-4o-mini", validation_alias=AliasChoices("OPENAI_MODEL", "openai_model"))
    ai_brain_model: str = Field(default="gpt-5.4-mini", validation_alias=AliasChoices("AI_BRAIN_MODEL", "ai_brain_model"))
    ai_brain_temperature: float = Field(default=0.2, validation_alias=AliasChoices("AI_BRAIN_TEMPERATURE", "ai_brain_temperature"))
    ai_humanize_model: str = Field(default="gpt-5.4-mini", validation_alias=AliasChoices("AI_HUMANIZE_MODEL", "ai_humanize_model"))
    openai_voice_model: str = Field(default="whisper-1", validation_alias=AliasChoices("OPENAI_VOICE_MODEL", "openai_voice_model"))
    ai_enabled: bool = Field(default=True, validation_alias=AliasChoices("AI_ENABLED", "ai_enabled"))
    bot_auto_reply_enabled: bool = Field(default=True, validation_alias=AliasChoices("BOT_AUTO_REPLY_ENABLED", "bot_auto_reply_enabled"))
    bot_activated_at: str = Field(default="2026-07-02T00:00:00+05:00", validation_alias=AliasChoices("BOT_ACTIVATED_AT", "bot_activated_at"))
    new_leads_only: bool = Field(default=True, validation_alias=AliasChoices("NEW_LEADS_ONLY", "new_leads_only"))
    openai_humanize_replies: bool = Field(default=True, validation_alias=AliasChoices("OPENAI_HUMANIZE_REPLIES", "openai_humanize_replies"))
    openai_brain_enabled: bool = Field(default=True, validation_alias=AliasChoices("OPENAI_BRAIN_ENABLED", "openai_brain_enabled"))
    openai_dialog_temperature: float = Field(default=0.2, validation_alias=AliasChoices("OPENAI_DIALOG_TEMPERATURE", "openai_dialog_temperature"))
    openai_humanize_temperature: float = Field(default=0.3, validation_alias=AliasChoices("OPENAI_HUMANIZE_TEMPERATURE", "openai_humanize_temperature"))
    openai_max_tokens: int = Field(default=700, validation_alias=AliasChoices("OPENAI_MAX_TOKENS", "openai_max_tokens"))
    # Потолок ответа брейна для gpt-5.x: в max_completion_tokens входят и токены
    # рассуждения, поэтому 700 может уйти на reasoning и обрезать JSON.
    ai_brain_max_completion_tokens: int = Field(default=2000, validation_alias=AliasChoices("AI_BRAIN_MAX_COMPLETION_TOKENS", "ai_brain_max_completion_tokens"))

    # Wazzup
    wazzup_api_key: str = Field(default="", validation_alias=AliasChoices("WAZZUP_API_KEY", "wazzup_api_key"))
    wazzup_channel_id: str = Field(default="", validation_alias=AliasChoices("WAZZUP_CHANNEL_ID", "wazzup_channel_id"))
    wazzup_allowed_channel_ids: str = Field(default="", validation_alias=AliasChoices("WAZZUP_ALLOWED_CHANNEL_IDS", "wazzup_allowed_channel_ids"))
    wazzup_instagram_channel_id: str = Field(default="", validation_alias=AliasChoices("WAZZUP_INSTAGRAM_CHANNEL_ID", "wazzup_instagram_channel_id"))
    # Алерт на расхождение каналов: если за окно доля входящих с
    # channel_id_mismatch выше порога, канал сменился и бот молчит для пациентов.
    # Замечать это надо сразу, а не постфактум по логам.
    channel_mismatch_alert_window_minutes: int = Field(default=15, validation_alias=AliasChoices("CHANNEL_MISMATCH_ALERT_WINDOW_MINUTES", "channel_mismatch_alert_window_minutes"))
    channel_mismatch_alert_min_messages: int = Field(default=5, validation_alias=AliasChoices("CHANNEL_MISMATCH_ALERT_MIN_MESSAGES", "channel_mismatch_alert_min_messages"))
    channel_mismatch_alert_ratio: float = Field(default=0.5, validation_alias=AliasChoices("CHANNEL_MISMATCH_ALERT_RATIO", "channel_mismatch_alert_ratio"))
    wazzup_api_url: str = Field(default="https://api.wazzup24.com/v3", validation_alias=AliasChoices("WAZZUP_API_URL", "wazzup_api_url"))
    wazzup_media_endpoint_template: str = Field(
        default="{api_url}/media/{file_id}",
        validation_alias=AliasChoices("WAZZUP_MEDIA_ENDPOINT_TEMPLATE", "wazzup_media_endpoint_template"),
    )

    # App / storage
    public_base_url: str = Field(
        default="https://neuro-balance-bot-main-production.up.railway.app",
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "RAILWAY_PUBLIC_DOMAIN", "public_base_url"),
    )
    deprecated_public_base_urls: str = Field(
        default="https://neuro-balance-bot-final-production.up.railway.app",
        validation_alias=AliasChoices("DEPRECATED_PUBLIC_BASE_URLS", "deprecated_public_base_urls"),
    )
    webhook_secret: str = Field(default="", validation_alias=AliasChoices("WEBHOOK_SECRET", "webhook_secret"))
    sqlite_path: str = Field(default="bot.sqlite3", validation_alias=AliasChoices("SQLITE_PATH", "sqlite_path"))

    # Slots
    slot_search_days: int = Field(default=7, validation_alias=AliasChoices("SLOT_SEARCH_DAYS", "slot_search_days"))
    max_slots_to_show: int = Field(default=5, validation_alias=AliasChoices("MAX_SLOTS_TO_SHOW", "max_slots_to_show"))

    # AI budget / style
    monthly_ai_budget_kzt: int = Field(default=20000, validation_alias=AliasChoices("MONTHLY_AI_BUDGET_KZT", "monthly_ai_budget_kzt"))
    # Реальный лимит трат в USD. По умолчанию 17.5 = $70 на 4 месяца работы.
    # Enforcement живёт в ai_budget.py: 80% — warning в логи, 100% — rule-based fallback.
    monthly_ai_budget_usd: float = Field(default=17.5, validation_alias=AliasChoices("MONTHLY_AI_BUDGET_USD", "monthly_ai_budget_usd"))
    ai_max_classifier_calls_per_day: int = Field(default=300, validation_alias=AliasChoices("AI_MAX_CLASSIFIER_CALLS_PER_DAY", "ai_max_classifier_calls_per_day"))
    operator_style_mode: bool = Field(default=True, validation_alias=AliasChoices("OPERATOR_STYLE_MODE", "operator_style_mode"))
    human_dialog_mode: bool = Field(default=True, validation_alias=AliasChoices("HUMAN_DIALOG_MODE", "human_dialog_mode"))

    # Work-hours guard: бот отвечает только ночью по бизнес-таймзоне.
    work_hours_guard_enabled: bool = Field(default=True, validation_alias=AliasChoices("WORK_HOURS_GUARD_ENABLED", "work_hours_guard_enabled"))
    bot_timezone: str = Field(default="Asia/Almaty", validation_alias=AliasChoices("BOT_TIMEZONE", "bot_timezone"))
    bot_work_start: str = Field(default="20:00", validation_alias=AliasChoices("BOT_WORK_START", "bot_work_start"))
    bot_work_end: str = Field(default="08:00", validation_alias=AliasChoices("BOT_WORK_END", "bot_work_end"))
    bot_test_window_enabled: bool = Field(default=False, validation_alias=AliasChoices("BOT_TEST_WINDOW_ENABLED", "bot_test_window_enabled"))
    bot_test_window_start: str = Field(default="14:00", validation_alias=AliasChoices("BOT_TEST_WINDOW_START", "bot_test_window_start"))
    bot_test_window_end: str = Field(default="14:30", validation_alias=AliasChoices("BOT_TEST_WINDOW_END", "bot_test_window_end"))
    bot_test_window_date: str = Field(default="2026-07-08", validation_alias=AliasChoices("BOT_TEST_WINDOW_DATE", "bot_test_window_date"))
    bot_active_from_hour: int = Field(default=20, validation_alias=AliasChoices("BOT_ACTIVE_FROM", "BOT_ACTIVE_FROM_HOUR", "bot_active_from_hour"))
    bot_active_until_hour: int = Field(default=8, validation_alias=AliasChoices("BOT_ACTIVE_TO", "BOT_ACTIVE_UNTIL_HOUR", "bot_active_until_hour"))
    bot_silent_outside_hours: bool = Field(default=True, validation_alias=AliasChoices("BOT_SILENT_OUTSIDE_HOURS", "bot_silent_outside_hours"))
    production_log_active_window_only: bool = Field(default=True, validation_alias=AliasChoices("PRODUCTION_LOG_ACTIVE_WINDOW_ONLY", "production_log_active_window_only"))
    message_debounce_seconds: int = Field(default=5, validation_alias=AliasChoices("MESSAGE_DEBOUNCE_SECONDS", "message_debounce_seconds"))
    timezone_offset_hours: int = Field(default=5, validation_alias=AliasChoices("TIMEZONE_OFFSET_HOURS", "timezone_offset_hours"))
    daytime_handoff_message: str = Field(
        default="Здравствуйте! Сейчас рабочее время контакт-центра, поэтому Ваше сообщение передано администратору. Он ответит Вам в порядке очереди🌿",
        validation_alias=AliasChoices("DAYTIME_HANDOFF_MESSAGE", "daytime_handoff_message"),
    )

    @field_validator("public_base_url", mode="after")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return str(value or "").strip().rstrip("/")

    @field_validator("deprecated_public_base_urls", mode="after")
    @classmethod
    def normalize_deprecated_public_base_urls(cls, value: str) -> str:
        urls = [url.strip().rstrip("/") for url in str(value or "").split(",") if url.strip()]
        return ",".join(urls)

    @field_validator("bot_active_from_hour", "bot_active_until_hour", mode="before")
    @classmethod
    def parse_hour_value(cls, value: Any) -> int:
        """Railway может хранить время как 20, 20:00 или 20/00.
        Для логики бота нужен только час: 20 => 20:00, 8 => 08:00.
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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
