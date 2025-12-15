from typing import Optional
from supabase import create_client, Client

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level singleton
_supabase_client: Optional[Client] = None


def get_supabase() -> Client:
    """Get or initialize Supabase client"""
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    # Validate settings
    if not settings.SUPABASE_URL:
        raise ValueError("SUPABASE_URL is not configured")
    if not settings.SUPABASE_KEY:
        raise ValueError("SUPABASE_KEY is not configured")

    try:
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        logger.info("Supabase client initialized successfully")
        return _supabase_client
    except Exception as e:
        logger.error(f"Failed to initialize Supabase: {e}")
        raise
