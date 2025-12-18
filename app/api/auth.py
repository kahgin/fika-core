"""
User Authentication API

Provides endpoints for user signup, login, logout, and profile management.
Uses Supabase for storage and bcrypt for password hashing.
"""

import bcrypt
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field, field_validator

from app.db.supabase_client import get_supabase
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Session duration
SESSION_DURATION_DAYS = 30

# Request/Response Models
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class SignupRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.lower().strip()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return v.lower().strip()


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    avatar: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(alias="currentPassword")
    new_password: str = Field(alias="newPassword")

    model_config = {"populate_by_name": True}

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    name: Optional[str]
    avatar: Optional[str]
    created_at: str


class AuthResponse(BaseModel):
    user: UserResponse
    token: str


# Helper Functions


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def create_session(user_id: str) -> str:
    """Create a new session for a user."""
    supabase = get_supabase()
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DURATION_DAYS)

    supabase.table("user_sessions").insert(
        {
            "user_id": user_id,
            "token": token,
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    return token


def get_user_from_token(token: str) -> Optional[dict]:
    """Get user from session token."""
    if not token:
        return None

    supabase = get_supabase()

    # Get valid session
    result = (
        supabase.table("user_sessions")
        .select("user_id, expires_at, is_valid")
        .eq("token", token)
        .eq("is_valid", True)
        .execute()
    )

    if not result.data:
        return None

    session = result.data[0]

    # Check expiration
    expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        # Invalidate expired session
        supabase.table("user_sessions").update({"is_valid": False}).eq("token", token).execute()
        return None

    # Get user
    user_result = supabase.table("users").select("*").eq("id", session["user_id"]).execute()

    if not user_result.data:
        return None

    return user_result.data[0]


def invalidate_session(token: str) -> bool:
    """Invalidate a session token."""
    supabase = get_supabase()
    supabase.table("user_sessions").update({"is_valid": False}).eq("token", token).execute()
    return True


def format_user_response(user: dict) -> UserResponse:
    """Format user data for response."""
    return UserResponse(
        id=str(user["id"]),
        email=user["email"],
        username=user["username"],
        name=user.get("name"),
        avatar=user.get("avatar"),
        created_at=user["created_at"],
    )


# API Endpoints


@router.post("/signup", response_model=AuthResponse)
def signup(request: SignupRequest):
    """
    Create a new user account.

    Returns user data and authentication token.
    """
    try:
        supabase = get_supabase()

        # Check if email already exists
        existing_email = supabase.table("users").select("id").eq("email", request.email).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Auto-generate username from email
        base_username = request.email.split("@")[0].lower()
        # Remove invalid characters
        base_username = "".join(c for c in base_username if c.isalnum() or c == "_")
        if len(base_username) < 3:
            base_username = base_username + "user"
        username = base_username[:30]

        # Check if this username exists and add suffix if needed
        existing = supabase.table("users").select("id").eq("username", username).execute()
        if existing.data:
            import random

            username = f"{username[:25]}_{random.randint(1000, 9999)}"

        # Create user
        password_hash = hash_password(request.password)
        result = (
            supabase.table("users")
            .insert(
                {
                    "email": request.email,
                    "username": username,
                    "password_hash": password_hash,
                    "name": request.name or username,
                }
            )
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create user")

        user = result.data[0]
        token = create_session(user["id"])

        # logger.info(f"New user signup: {request.email}")

        return AuthResponse(
            user=format_user_response(user),
            token=token,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Signup error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/login", response_model=AuthResponse)
def login(request: LoginRequest):
    """
    Authenticate user and return token.
    """
    try:
        supabase = get_supabase()

        # Find user by email
        result = supabase.table("users").select("*").eq("email", request.email).execute()

        if not result.data:
            raise HTTPException(
                status_code=401,
                detail="We couldn't find an account with that email. Please check and try again.",
            )

        user = result.data[0]

        # Verify password
        if not verify_password(request.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Incorrect password. Please try again.")

        # Check if user is active
        if not user.get("is_active", True):
            raise HTTPException(status_code=401, detail="Account is disabled")

        # Create session
        token = create_session(user["id"])

        # logger.info(f"User login: {request.email}")

        return AuthResponse(
            user=format_user_response(user),
            token=token,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Login error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    """
    Logout user by invalidating their session token.
    """
    if not authorization:
        return {"status": "ok"}

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    invalidate_session(token)

    return {"status": "ok"}


@router.get("/me", response_model=UserResponse)
def get_current_user(authorization: Optional[str] = Header(None)):
    """
    Get current authenticated user's profile.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    user = get_user_from_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return format_user_response(user)


@router.put("/me", response_model=UserResponse)
def update_profile(request: UpdateProfileRequest, authorization: Optional[str] = Header(None)):
    """
    Update current user's profile.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    user = get_user_from_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        supabase = get_supabase()

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.avatar is not None:
            update_data["avatar"] = request.avatar

        if update_data:
            result = supabase.table("users").update(update_data).eq("id", user["id"]).execute()
            if result.data:
                user = result.data[0]

        return format_user_response(user)

    except Exception as e:
        logger.exception(f"Update profile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/change-password")
def change_password(request: ChangePasswordRequest, authorization: Optional[str] = Header(None)):
    """
    Change current user's password.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    user = get_user_from_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Verify current password
    if not verify_password(request.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    try:
        supabase = get_supabase()
        new_hash = hash_password(request.new_password)
        supabase.table("users").update({"password_hash": new_hash}).eq("id", user["id"]).execute()

        # Invalidate all other sessions (keep current one)
        supabase.table("user_sessions").update({"is_valid": False}).eq("user_id", user["id"]).neq(
            "token", token
        ).execute()

        # logger.info(f"Password changed for user: {user['email']}")

        return {"status": "ok"}

    except Exception as e:
        logger.exception(f"Change password error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/me")
def delete_account(authorization: Optional[str] = Header(None)):
    """
    Delete current user's account and all associated data.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    user = get_user_from_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        supabase = get_supabase()
        supabase.table("users").delete().eq("id", user["id"]).execute()

        # logger.info(f"Account deleted: {user['email']}")

        return {"status": "deleted"}

    except Exception as e:
        logger.exception(f"Delete account error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Utility function for other modules to get user from request
def get_optional_user_id(authorization: Optional[str]) -> Optional[str]:
    """
    Get user ID from authorization header if valid, None otherwise.
    Used for endpoints that work for both authenticated and anonymous users.
    """
    if not authorization:
        return None

    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    user = get_user_from_token(token)

    return user["id"] if user else None
