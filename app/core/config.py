from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    SUPABASE_URL: str
    SUPABASE_KEY: str
    GOOGLE_MAPS_API_KEY: str
    GOOGLE_AI_STUDIO_KEY: str
    OSRM_URL: str
    UNSPLASH_APPLICATION_ID: str
    UNSPLASH_ACCESS_KEY: str
    UNSPLASH_SECRET_KEY: str

    CORS_ORIGINS: list[str] = ["*"]

    DEFAULT_LIMIT: int = 12
    MAX_LIMIT: int = 90


settings = Settings()
