from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    SUPABASE_URL: str = Field(..., env="SUPABASE_URL")
    SUPABASE_KEY: str = Field(..., env="SUPABASE_KEY")
    GOOGLE_MAPS_API_KEY: str = Field(..., env="GOOGLE_MAPS_API_KEY")
    OPENAI_API_KEY: str = Field(..., env="OPENAI_API_KEY")

    # CORS and app constants
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "https://fikatrip.vercel.app"]
    DEFAULT_LIMIT: int = 12
    MAX_LIMIT: int = 90

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
