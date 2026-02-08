"""Robot Framework library for authentication helpers.

Provides utilities for working with JWT tokens and user authentication.
"""

import base64
import json


def get_user_id_from_token(jwt_token: str) -> str:
    """Extract user ID from JWT token.

    Args:
        jwt_token: JWT token string (format: header.payload.signature)

    Returns:
        User ID from the 'sub' field in the token payload

    Example:
        ${user_id}=    Get User ID From Token    ${token}
    """
    # Split token into parts
    parts = jwt_token.split('.')
    if len(parts) != 3:
        raise ValueError(f"Invalid JWT token format: expected 3 parts, got {len(parts)}")

    # Decode payload (add padding if needed)
    payload_b64 = parts[1]
    padding = (4 - len(payload_b64) % 4) % 4
    payload_b64_padded = payload_b64 + ('=' * padding)

    # Base64 decode and parse JSON
    payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
    payload = json.loads(payload_bytes.decode('utf-8'))

    # Extract user ID from 'sub' field
    user_id = payload.get('sub')
    if not user_id:
        raise ValueError("Token payload does not contain 'sub' field")

    return user_id
