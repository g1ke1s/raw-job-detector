from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_session_string: str

    gemini_api_key: str = ""
    gemini_api_key_2: str = ""
    groq_api_key: str = ""
    groq_api_key_2: str = ""
    mistral_api_key: str = ""
    openrouter_api_key: str = ""

    database_url: str

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()