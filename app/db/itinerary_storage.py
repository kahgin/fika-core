"""
Itinerary storage using Supabase.

This module provides CRUD operations for itineraries stored in Supabase.
It can work alongside the file-based storage as a fallback.
"""

from typing import Optional

from app.db.supabase_client import get_supabase
from app.utils.logger import get_logger

logger = get_logger(__name__)


def save_itinerary_to_db(itin_id: str, data: dict) -> bool:
    """
    Save itinerary to Supabase database.

    Args:
        itin_id: The itinerary UUID
        data: The full itinerary data (meta + plan)

    Returns:
        True if successful, False otherwise
    """
    try:
        supabase = get_supabase()

        meta = data.get("meta", {})
        plan = data.get("plan", {})
        status = data.get("status", "success")

        # Log what we're saving
        days_summary = [[s.get('poi_id') for s in d.get('stops', [])] for d in plan.get('days', [])]

        # Use direct table upsert for reliability
        
        result = supabase.table("itineraries").upsert(
            {
                "id": itin_id,
                "title": meta.get("title"),
                "destinations": meta.get("destinations", []),
                "dates": meta.get("dates", {}),
                "travelers": meta.get("travelers", {}),
                "preferences": meta.get("preferences", {}),
                "flags": meta.get("flags", {}),
                "hotels": meta.get("hotels", []),
                "mandatory_pois": meta.get("mandatory_pois", []),
                "ideas": meta.get("ideas", []),
                "plan": plan,
                "user_id": meta.get("user_id"),
                "status": status if status != "success" else "active",
            }
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to save itinerary {itin_id} to Supabase: {e}")
        return False


def load_itinerary_from_db(itin_id: str) -> Optional[dict]:
    """
    Load itinerary from Supabase database.

    Args:
        itin_id: The itinerary UUID

    Returns:
        The itinerary data dict, or None if not found
    """
    try:
        supabase = get_supabase()

        # Use direct table access instead of RPC for reliability
        result = supabase.table("itineraries").select("*").eq("id", itin_id).execute()

        if not result.data or len(result.data) == 0:
            logger.warning(f"Itinerary {itin_id} not found in database")
            return None

        row = result.data[0]

        # Reconstruct the data structure to match the original format
        # The plan field already contains the full plan structure
        plan = row.get("plan", {})

        data = {
            "itin_id": str(row.get("id")),
            "status": row.get("status", "success"),
            "meta": {
                "title": row.get("title"),
                "destinations": row.get("destinations", []),
                "dates": row.get("dates", {}),
                "num_days": row.get("dates", {}).get("days")
                or len(plan.get("days", [])),
                "travelers": row.get("travelers", {}),
                "preferences": row.get("preferences", {}),
                "flags": row.get("flags", {}),
                "hotels": row.get("hotels", []),
                "mandatory_pois": row.get("mandatory_pois", []),
                "ideas": row.get("ideas", []),
                "user_id": row.get("user_id"),
            },
            "plan": plan,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

        return data
    except Exception as e:
        logger.error(f"Failed to load itinerary {itin_id} from Supabase: {e}")
        return None


def delete_itinerary_from_db(itin_id: str) -> bool:
    """
    Delete itinerary from Supabase database.

    Args:
        itin_id: The itinerary UUID

    Returns:
        True if successful, False otherwise
    """
    try:
        supabase = get_supabase()

        # Use direct table delete
        supabase.table("itineraries").delete().eq("id", itin_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to delete itinerary {itin_id} from Supabase: {e}")
        return False


def soft_delete_itinerary_for_user(
    itin_id: str, user_id: Optional[str]
) -> tuple[bool, str]:
    """
    Soft-delete itinerary (set status='deleted') with ownership verification using RPC.

    Args:
        itin_id: The itinerary UUID
        user_id: The user ID for ownership check (None for guest)

    Returns:
        Tuple of (success, error_code)
        - (True, "") on success
        - (False, "not_found") if itinerary doesn't exist
        - (False, "forbidden") if user doesn't have access
    """
    try:
        supabase = get_supabase()

        result = supabase.rpc(
            "rpc_delete_itinerary_for_user",
            {"p_itinerary_id": itin_id, "p_user_id": user_id},
        ).execute()

        if not result.data or len(result.data) == 0:
            return False, "error"

        row = result.data[0]
        if not row.get("success"):
            error_code = row.get("error_code", "error")
            if error_code == "forbidden":
                logger.warning(
                    f"User {user_id} attempted to delete itinerary {itin_id} - forbidden"
                )
            return False, error_code
        return True, ""
    except Exception as e:
        logger.error(f"Failed to soft-delete itinerary {itin_id}: {e}")
        return False, "error"


def list_itineraries_from_db(
    user_id: Optional[str] = None,
    status: str = "active",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    List itineraries from Supabase database using RPC when user_id provided.

    Args:
        user_id: Optional user ID to filter by
        status: Status filter (default: 'active')
        limit: Maximum number of results
        offset: Offset for pagination

    Returns:
        Tuple of (list of itineraries, total count)
    """
    try:
        supabase = get_supabase()

        if user_id:
            result = supabase.rpc(
                "rpc_list_user_itineraries",
                {
                    "p_user_id": user_id,
                    "p_status": status,
                    "p_limit": limit,
                    "p_offset": offset,
                },
            ).execute()

            if not result.data:
                return [], 0

            itineraries = []
            total_count = 0
            for row in result.data:
                total_count = row.get("total_count", 0)
                itineraries.append(
                    {
                        "id": row.get("id"),
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                        "title": row.get("title"),
                        "destinations": row.get("destinations", []) or [],
                        "dates": row.get("dates", {}) or {},
                        "status": row.get("status"),
                    }
                )
            return itineraries, total_count

        # Fallback to direct query when no user_id (admin use case)
        query = supabase.table("itineraries").select("*", count="exact")

        if status:
            query = query.eq("status", status)

        query = query.order("updated_at", desc=True).range(offset, offset + limit - 1)

        result = query.execute()

        if not result.data:
            return [], 0

        total_count = result.count or 0

        itineraries = []
        for row in result.data:
            itineraries.append(
                {
                    "id": row.get("id"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "title": row.get("title"),
                    "destinations": row.get("destinations", []),
                    "dates": row.get("dates", {}),
                    "status": row.get("status"),
                }
            )

        return itineraries, total_count
    except Exception as e:
        logger.error(f"Failed to list itineraries from Supabase: {e}")
        return [], 0


def update_itinerary_meta_for_user(
    itin_id: str, meta_updates: dict, user_id: Optional[str]
) -> tuple[bool, str]:
    """
    Update itinerary meta fields with ownership verification using RPC.

    Args:
        itin_id: The itinerary UUID
        meta_updates: Dict of meta fields to update
        user_id: The user ID for ownership check (None for guest)

    Returns:
        Tuple of (success, error_code)
        - (True, "") on success
        - (False, "not_found") if itinerary doesn't exist
        - (False, "forbidden") if user doesn't have access
    """
    try:
        supabase = get_supabase()

        result = supabase.rpc(
            "rpc_update_itinerary_meta",
            {
                "p_itinerary_id": itin_id,
                "p_user_id": user_id,
                "p_title": meta_updates.get("title"),
                "p_dates": meta_updates.get("dates"),
                "p_travelers": meta_updates.get("travelers"),
                "p_preferences": meta_updates.get("preferences"),
                "p_flags": meta_updates.get("flags"),
            },
        ).execute()

        if not result.data or len(result.data) == 0:
            return False, "error"

        row = result.data[0]
        if not row.get("success"):
            error_code = row.get("error_code", "error")
            if error_code == "forbidden":
                logger.warning(
                    f"User {user_id} attempted to update itinerary {itin_id} - forbidden"
                )
            return False, error_code

        return True, ""
    except Exception as e:
        logger.error(f"Failed to update meta for itinerary {itin_id}: {e}")
        return False, "error"


def update_itinerary_plan_for_user(
    itin_id: str, plan: dict, user_id: Optional[str]
) -> tuple[bool, str]:
    """
    Update itinerary plan with ownership verification using RPC.

    Args:
        itin_id: The itinerary UUID
        plan: The new plan data
        user_id: The user ID for ownership check (None for guest)

    Returns:
        Tuple of (success, error_code)
    """
    try:
        supabase = get_supabase()

        result = supabase.rpc(
            "rpc_update_itinerary_plan",
            {
                "p_itinerary_id": itin_id,
                "p_user_id": user_id,
                "p_plan": plan,
            },
        ).execute()

        if not result.data or len(result.data) == 0:
            return False, "error"

        row = result.data[0]
        if not row.get("success"):
            error_code = row.get("error_code", "error")
            if error_code == "forbidden":
                logger.warning(
                    f"User {user_id} attempted to update plan for itinerary {itin_id} - forbidden"
                )
            return False, error_code

        return True, ""
    except Exception as e:
        logger.error(f"Failed to update plan for itinerary {itin_id}: {e}")
        return False, "error"


def update_itinerary_meta_in_db(itin_id: str, meta_updates: dict) -> bool:
    """
    Update only the meta fields of an itinerary.

    Args:
        itin_id: The itinerary UUID
        meta_updates: Dict of meta fields to update

    Returns:
        True if successful, False otherwise
    """
    try:
        supabase = get_supabase()

        # Build update data - only include provided fields
        update_data = {}

        if "title" in meta_updates:
            update_data["title"] = meta_updates["title"]
        if "destinations" in meta_updates:
            update_data["destinations"] = meta_updates["destinations"]
        if "dates" in meta_updates:
            update_data["dates"] = meta_updates["dates"]
        if "travelers" in meta_updates:
            update_data["travelers"] = meta_updates["travelers"]
        if "preferences" in meta_updates:
            update_data["preferences"] = meta_updates["preferences"]
        if "flags" in meta_updates:
            update_data["flags"] = meta_updates["flags"]
        if "hotels" in meta_updates:
            update_data["hotels"] = meta_updates["hotels"]
        if "mandatory_pois" in meta_updates:
            update_data["mandatory_pois"] = meta_updates["mandatory_pois"]
        if "ideas" in meta_updates:
            update_data["ideas"] = meta_updates["ideas"]

        supabase.table("itineraries").update(update_data).eq("id", itin_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to update meta for itinerary {itin_id} in Supabase: {e}")
        return False


def update_itinerary_plan_in_db(itin_id: str, plan: dict) -> bool:
    """
    Update only the plan of an itinerary.

    Args:
        itin_id: The itinerary UUID
        plan: The new plan data

    Returns:
        True if successful, False otherwise
    """
    try:
        supabase = get_supabase()
        supabase.table("itineraries").update({"plan": plan}).eq("id", itin_id).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to update plan for itinerary {itin_id} in Supabase: {e}")
        return False


def itinerary_exists_in_db(itin_id: str) -> bool:
    """
    Check if an itinerary exists in the database.

    Args:
        itin_id: The itinerary UUID

    Returns:
        True if exists, False otherwise
    """
    try:
        supabase = get_supabase()

        result = supabase.table("itineraries").select("id").eq("id", itin_id).execute()

        return result.data is not None and len(result.data) > 0
    except Exception as e:
        logger.error(f"Failed to check itinerary existence {itin_id}: {e}")
        return False


def load_itinerary_for_user(
    itin_id: str, user_id: Optional[str]
) -> tuple[Optional[dict], str]:
    """
    Load itinerary from database with ownership verification using RPC.

    Args:
        itin_id: The itinerary UUID
        user_id: The user ID to verify ownership (None for anonymous/guest)

    Returns:
        Tuple of (itinerary data or None, error message)
        - If found and accessible: (data, "")
        - If not found: (None, "not_found")
        - If user doesn't own it: (None, "forbidden")
    """
    try:
        supabase = get_supabase()

        result = supabase.rpc(
            "rpc_get_itinerary_for_user",
            {"p_itinerary_id": itin_id, "p_user_id": user_id},
        ).execute()

        if not result.data or len(result.data) == 0:
            return None, "not_found"

        row = result.data[0]
        access_status = row.get("access_status")

        if access_status == "not_found":
            return None, "not_found"
        if access_status == "forbidden":
            logger.warning(
                f"User {user_id} attempted to access itinerary {itin_id} - forbidden"
            )
            return None, "forbidden"

        # Reconstruct the data structure
        plan = row.get("plan", {}) or {}

        data = {
            "itin_id": str(row.get("id")),
            "status": row.get("status", "success"),
            "meta": {
                "title": row.get("title"),
                "destinations": row.get("destinations", []) or [],
                "dates": row.get("dates", {}) or {},
                "num_days": (row.get("dates") or {}).get("days")
                or len(plan.get("days", [])),
                "travelers": row.get("travelers", {}) or {},
                "preferences": row.get("preferences", {}) or {},
                "flags": row.get("flags", {}) or {},
                "hotels": row.get("hotels", []) or [],
                "mandatory_pois": row.get("mandatory_pois", []) or [],
                "ideas": row.get("ideas", []) or [],
                "user_id": row.get("user_id"),
            },
            "plan": plan,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

        return data, ""
    except Exception as e:
        logger.error(f"Failed to load itinerary {itin_id} for user {user_id}: {e}")
        return None, "error"


def claim_itinerary_for_user(itin_id: str, user_id: str) -> tuple[bool, str]:
    """
    Claim an orphaned itinerary for a user using RPC.

    Args:
        itin_id: The itinerary UUID
        user_id: The user ID to claim the itinerary

    Returns:
        Tuple of (success, error_code)
        - (True, "") on success
        - (True, "already_owned") if user already owns it
        - (False, "not_found") if itinerary doesn't exist
        - (False, "forbidden") if itinerary belongs to another user
        - (False, "invalid_user") if user_id is None
    """
    try:
        supabase = get_supabase()

        result = supabase.rpc(
            "rpc_claim_itinerary",
            {"p_itinerary_id": itin_id, "p_user_id": user_id},
        ).execute()

        if not result.data or len(result.data) == 0:
            return False, "error"

        row = result.data[0]
        error_code = row.get("error_code", "")

        if not row.get("success"):
            if error_code == "forbidden":
                logger.warning(
                    f"User {user_id} attempted to claim itinerary {itin_id} - forbidden"
                )
            return False, error_code

        if error_code == "already_owned":
            return True, "already_owned"
        return True, ""
    except Exception as e:
        logger.error(f"Failed to claim itinerary {itin_id} for user {user_id}: {e}")
        return False, "error"
