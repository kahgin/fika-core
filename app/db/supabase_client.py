from supabase import create_client, Client
from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

supabase: Client = None


def init_supabase():
    global supabase
    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        try:
            supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
            logger.info("Supabase client initialized successfully")
            return supabase
        except Exception as e:
            logger.error(f"Failed to initialize Supabase: {e}")
            return None
    else:
        logger.error("SUPABASE_URL or SUPABASE_KEY not set")
        return None


# Initialize Supabase client
supabase = init_supabase()


def get_supabase() -> Client:
    if supabase is None:
        raise Exception("Supabase not connected")
    return supabase
