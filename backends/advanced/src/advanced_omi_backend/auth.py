"""Authentication configuration for fastapi-users with email/password and JWT."""

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Literal, Optional, overload

import jwt
from beanie import PydanticObjectId
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    CookieTransport,
    JWTStrategy,
)

from advanced_omi_backend.users import User, UserCreate, get_user_db

logger = logging.getLogger(__name__)

load_dotenv()
# JWT configuration
JWT_LIFETIME_SECONDS = int(os.getenv("JWT_LIFETIME_SECONDS", "86400")) # 24 hours


@overload
def _verify_configured(var_name: str, *, optional: Literal[False] = False) -> str: ...
@overload
def _verify_configured(var_name: str, *, optional: Literal[True]) -> Optional[str]: ...


def _verify_configured(var_name: str, *, optional: bool = False) -> Optional[str]:
    value = os.getenv(var_name)
    if not optional and not value:
        raise ValueError(f"{var_name} is not set")
    return value


# Configuration from environment variables
SECRET_KEY = _verify_configured("AUTH_SECRET_KEY")
COOKIE_SECURE = _verify_configured("COOKIE_SECURE", optional=True) == "true"

# Admin user configuration
ADMIN_PASSWORD = _verify_configured("ADMIN_PASSWORD")
ADMIN_EMAIL = _verify_configured("ADMIN_EMAIL", optional=True) or "admin@example.com"

# Accepted token issuers - comma-separated list of services whose tokens we accept
# Default: "chronicle,ushadow" (accept tokens from both chronicle and ushadow)
ACCEPTED_ISSUERS = [
    iss.strip() 
    for iss in os.getenv("ACCEPTED_TOKEN_ISSUERS", "chronicle,ushadow").split(",") 
    if iss.strip()
]
logger.info(f"Accepting tokens from issuers: {ACCEPTED_ISSUERS}")


class UserManager(BaseUserManager[User, PydanticObjectId]):
    """User manager with minimal customization for fastapi-users."""

    reset_password_token_secret = SECRET_KEY
    verification_token_secret = SECRET_KEY

    def parse_id(self, value: str) -> PydanticObjectId:
        """Parse string ID to PydanticObjectId for MongoDB compatibility."""
        try:
            return PydanticObjectId(value)
        except Exception as e:
            raise ValueError(f"Invalid ObjectId format: {value}") from e

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        """Called after a user registers."""
        logger.info(f"User {user.user_id} ({user.email}) has registered.")

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """Called after a user requests password reset."""
        logger.info(f"User {user.user_id} ({user.email}) has requested password reset")

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """Called after a user requests verification."""
        logger.info(f"Verification requested for user {user.user_id} ({user.email})")


async def get_user_manager(user_db=Depends(get_user_db)):
    """Get user manager instance for dependency injection."""
    yield UserManager(user_db)


# Transport configurations
cookie_transport = CookieTransport(
    cookie_max_age=JWT_LIFETIME_SECONDS,  # Matches JWT lifetime
    cookie_secure=COOKIE_SECURE,  # Set to False in development if not using HTTPS
    cookie_httponly=True,
    cookie_samesite="lax",
)

bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    """Get JWT strategy for token generation and validation.
    
    Configures token_audience to accept:
    - fastapi-users:auth (Chronicle-generated tokens)
    - ushadow (cross-service tokens from ushadow)
    - chronicle (tokens intended for Chronicle)
    """
    return JWTStrategy(
        secret=SECRET_KEY, 
        lifetime_seconds=JWT_LIFETIME_SECONDS,
        token_audience=["fastapi-users:auth", "ushadow", "chronicle"],
    )


def validate_token_issuer(token: str) -> bool:
    """Validate that a token was issued by an accepted issuer.
    
    Args:
        token: JWT token string
        
    Returns:
        True if token issuer is in ACCEPTED_ISSUERS, False otherwise
    """
    try:
        # Decode without verification to check issuer
        payload = jwt.decode(token, options={"verify_signature": False})
        issuer = payload.get("iss")
        if issuer and issuer in ACCEPTED_ISSUERS:
            return True
        # Also accept tokens without issuer (legacy tokens)
        if issuer is None:
            return True
        logger.warning(f"Token rejected: issuer '{issuer}' not in {ACCEPTED_ISSUERS}")
        return False
    except Exception as e:
        logger.error(f"Error validating token issuer: {e}")
        return False


async def validate_cross_service_token(token: str) -> Optional[User]:
    """Validate a cross-service JWT token and return the user.
    
    This handles tokens issued by other services (like ushadow) that have
    custom audience claims. Unlike fastapi-users' JWTStrategy which expects
    audience=["fastapi-users:auth"], this accepts tokens with audience
    containing "chronicle" or "ushadow".
    
    Args:
        token: JWT token string
        
    Returns:
        User if token is valid and user exists, None otherwise
    """
    try:
        # First decode without verification to check claims
        unverified = jwt.decode(token, options={"verify_signature": False})
        issuer = unverified.get("iss")
        audience = unverified.get("aud")
        
        logger.debug(f"Cross-service token: iss={issuer}, aud={audience}")
        
        # Check issuer
        if issuer and issuer not in ACCEPTED_ISSUERS:
            logger.warning(f"Token rejected: issuer '{issuer}' not in {ACCEPTED_ISSUERS}")
            return None
        
        # Determine which audience to verify against
        # Accept tokens intended for "chronicle" or any accepted issuer
        verify_audience = None
        if isinstance(audience, list):
            # Find an acceptable audience from the token's audience list
            for aud in audience:
                if aud in ACCEPTED_ISSUERS or aud == "chronicle":
                    verify_audience = aud
                    break
        elif isinstance(audience, str):
            if audience in ACCEPTED_ISSUERS or audience == "chronicle":
                verify_audience = audience
        
        # Now decode with full verification
        try:
            if verify_audience:
                payload = jwt.decode(
                    token, 
                    SECRET_KEY, 
                    algorithms=["HS256"],
                    audience=verify_audience
                )
            else:
                # No audience or unrecognized - decode without audience check
                payload = jwt.decode(
                    token, 
                    SECRET_KEY, 
                    algorithms=["HS256"],
                    options={"verify_aud": False}
                )
        except jwt.ExpiredSignatureError:
            logger.warning("Cross-service token expired")
            return None
        except jwt.InvalidSignatureError:
            logger.warning("Cross-service token has invalid signature")
            return None
        except jwt.InvalidAudienceError as e:
            logger.warning(f"Cross-service token audience mismatch: {e}")
            return None
        
        # Get user ID from token
        user_id = payload.get("sub")
        if not user_id:
            logger.warning("Token missing 'sub' claim")
            return None
        
        # Look up user in database
        try:
            user_db_gen = get_user_db()
            user_db = await user_db_gen.__anext__()
            
            # Parse user ID to ObjectId
            from beanie import PydanticObjectId
            try:
                oid = PydanticObjectId(user_id)
            except Exception:
                logger.warning(f"Invalid user ID format in token: {user_id}")
                return None
            
            user = await user_db.get(oid)
            if user and user.is_active:
                logger.info(f"Cross-service auth successful for user {user.user_id} ({user.email})")
                return user
            elif user:
                logger.warning(f"User {user_id} exists but is inactive")
            else:
                logger.warning(f"User {user_id} not found in database")
            
        except Exception as e:
            logger.error(f"Error looking up user from token: {e}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error validating cross-service token: {e}")
        return None


def generate_jwt_for_user(
    user_id: str, 
    user_email: str,
    audiences: list[str] = None
) -> str:
    """Generate a JWT token for cross-service authentication.

    Creates a JWT token that can be used to authenticate with any service 
    that shares the same AUTH_SECRET_KEY and accepts this issuer.

    Note: ushadow is the central auth provider. Chronicle can still issue
    tokens for backward compatibility, but new integrations should use ushadow.

    Args:
        user_id: User's unique identifier (MongoDB ObjectId as string)
        user_email: User's email address
        audiences: List of services this token is valid for.
                   Defaults to accepted issuers from ACCEPTED_TOKEN_ISSUERS env var.

    Returns:
        JWT token string valid for JWT_LIFETIME_SECONDS (default: 24 hours)
    """
    if audiences is None:
        audiences = ACCEPTED_ISSUERS.copy()
    
    payload = {
        "sub": user_id,
        "email": user_email,
        "iss": "chronicle",  # This service is the issuer
        "aud": audiences,
        "exp": datetime.utcnow() + timedelta(seconds=JWT_LIFETIME_SECONDS),
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    return token


# Authentication backends
cookie_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

bearer_backend = AuthenticationBackend(
    name="bearer",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

# FastAPI Users instance
fastapi_users = FastAPIUsers[User, PydanticObjectId](
    get_user_manager,
    [cookie_backend, bearer_backend],
)

# HTTP Bearer scheme for extracting tokens
_optional_bearer = HTTPBearer(auto_error=False)

# Internal fastapi-users dependencies (used as fallback)
_fastapi_users_active = fastapi_users.current_user(active=True)
_fastapi_users_optional = fastapi_users.current_user(active=True, optional=True)


async def current_active_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> User:
    """
    Combined auth dependency that supports both cross-service and native tokens.

    This replaces the default fastapi-users current_user to support tokens from
    ushadow (with aud=["ushadow", "chronicle"]) as well as Chronicle-native tokens.

    All endpoints using current_active_user automatically get cross-service support.

    Raises:
        HTTPException(401): If no valid token found
    """
    # Try Bearer token first (cross-service)
    if credentials:
        token = credentials.credentials
        user = await validate_cross_service_token(token)
        if user:
            logger.debug(f"Cross-service auth successful for user {user.user_id}")
            return user

    # Try cookie authentication (Chronicle-native)
    try:
        cookie_token = request.cookies.get("fastapiusersauth")
        if cookie_token:
            # Try cross-service validation first (in case it's from ushadow)
            user = await validate_cross_service_token(cookie_token)
            if user:
                logger.debug(f"Cross-service cookie auth for user {user.user_id}")
                return user

            # Fall back to fastapi-users strategy for native Chronicle tokens
            strategy = get_jwt_strategy()
            user_db_gen = get_user_db()
            user_db = await user_db_gen.__anext__()
            user_manager = UserManager(user_db)
            user = await strategy.read_token(cookie_token, user_manager)
            if user and user.is_active:
                logger.debug(f"Native cookie auth for user {user.user_id}")
                return user
    except Exception as e:
        logger.warning(f"Cookie auth failed: {e}")

    logger.warning("Authentication failed - no valid token")
    raise HTTPException(status_code=401, detail="Authentication required")


async def current_active_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> Optional[User]:
    """Optional version - returns None instead of raising 401."""
    try:
        return await current_active_user(request, credentials)
    except HTTPException:
        return None


current_superuser = fastapi_users.current_user(active=True, superuser=True)


async def get_user_from_token_param(token: str) -> Optional[User]:
    """
    Get user from JWT token string (for query parameter authentication).

    This is useful for endpoints that need to support token-based auth via query params,
    such as HTML audio elements that can't set custom headers.

    Args:
        token: JWT token string

    Returns:
        User object if token is valid and user is active, None otherwise
    """
    if not token:
        return None
    try:
        strategy = get_jwt_strategy()
        user_db_gen = get_user_db()
        user_db = await user_db_gen.__anext__()
        user_manager = UserManager(user_db)
        user = await strategy.read_token(token, user_manager)
        if user and user.is_active:
            return user
    except Exception:
        pass
    return None


def get_accessible_user_ids(user: User) -> list[str] | None:
    """
    Get list of user IDs that the current user can access data for.
    Returns None for superusers (can access all), or [user.id] for regular users.
    """
    if user.is_superuser:
        return None  # Can access all data
    else:
        return [str(user.id)]  # Can only access own data


async def create_admin_user_if_needed():
    """Create admin user during startup if it doesn't exist and credentials are provided."""
    if not ADMIN_PASSWORD:
        logger.warning("Skipping admin user creation - ADMIN_PASSWORD not set")
        return

    try:
        # Get user database
        user_db_gen = get_user_db()
        user_db = await user_db_gen.__anext__()

        # Check if admin user already exists by email
        existing_admin = await user_db.get_by_email(ADMIN_EMAIL)

        if existing_admin:
            logger.info(
                f"✅ Admin user already exists: {existing_admin.user_id} ({existing_admin.email})"
            )
            return

        # Create admin user
        user_manager_gen = get_user_manager(user_db)
        user_manager = await user_manager_gen.__anext__()

        admin_create = UserCreate(
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            is_superuser=True,
            is_verified=True,
            display_name="Administrator",
        )

        admin_user = await user_manager.create(admin_create)
        logger.info(
            f"✅ Created admin user: {admin_user.user_id} ({admin_user.email}) (ID: {admin_user.id})"
        )

    except Exception as e:
        logger.error(f"Failed to create admin user: {e}", exc_info=True)


async def websocket_auth(websocket, token: Optional[str] = None) -> Optional[User]:
    """
    WebSocket authentication that supports both cookie and token-based auth.
    
    Supports cross-service tokens from ushadow with custom audience claims,
    as well as Chronicle's own cookies.
    
    Returns None if authentication fails (allowing graceful handling).
    """
    # Try JWT token from query parameter first (cross-service auth)
    if token:
        logger.info(f"Attempting WebSocket auth with query token (first 20 chars): {token[:20]}...")
        
        # Use cross-service validation which handles custom audiences
        user = await validate_cross_service_token(token)
        if user:
            logger.info(f"WebSocket auth successful for user {user.user_id} using cross-service token.")
            return user
        else:
            logger.warning("Cross-service token validation failed, trying cookie auth...")

    # Try cookie authentication (Chronicle's own auth)
    logger.debug("Attempting WebSocket auth with cookie.")
    try:
        cookie_header = next(
            (v.decode() for k, v in websocket.headers.items() if k.lower() == b"cookie"), None
        )
        if cookie_header:
            match = re.search(r"fastapiusersauth=([^;]+)", cookie_header)
            if match:
                cookie_token = match.group(1)
                # Try cross-service validation for cookie too (in case it's from ushadow)
                user = await validate_cross_service_token(cookie_token)
                if user:
                    logger.info(f"WebSocket auth successful for user {user.user_id} using cookie.")
                    return user
                    
                # Fall back to fastapi-users strategy for native Chronicle tokens
                strategy = get_jwt_strategy()
                user_db_gen = get_user_db()
                user_db = await user_db_gen.__anext__()
                user_manager = UserManager(user_db)
                user = await strategy.read_token(cookie_token, user_manager)
                if user and user.is_active:
                    logger.info(f"WebSocket auth successful for user {user.user_id} using native cookie.")
                    return user
    except Exception as e:
        logger.warning(f"WebSocket auth with cookie failed: {e}")

    logger.warning("WebSocket authentication failed.")
    return None
