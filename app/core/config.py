from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
    )

    SUPABASE_URL: str
    SUPABASE_KEY: str
    GOOGLE_MAPS_API_KEY: str
    OPENAI_API_KEY: str
    OSRM_URL: str

    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "https://fikatrip.vercel.app",
    ]

    DEFAULT_LIMIT: int = 12
    MAX_LIMIT: int = 90


settings = Settings()
