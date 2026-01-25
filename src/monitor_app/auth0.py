"""Auth0 JWT validation utilities for MCP OAuth 2.1 authentication."""

import logging
from functools import lru_cache

import requests
from django.conf import settings
from jose import jwt, JWTError

logger = logging.getLogger(__name__)

# Cache JWKS for 1 hour (3600 seconds)
_jwks_cache = {"keys": None, "expires": 0}


def get_jwks():
    """Fetch and cache JWKS from Auth0."""
    import time

    now = time.time()
    if _jwks_cache["keys"] and now < _jwks_cache["expires"]:
        return _jwks_cache["keys"]

    if not settings.AUTH0_DOMAIN:
        logger.warning("AUTH0_DOMAIN not configured")
        return None

    jwks_url = f"https://{settings.AUTH0_DOMAIN}/.well-known/jwks.json"
    try:
        response = requests.get(jwks_url, timeout=10)
        response.raise_for_status()
        jwks = response.json()
        _jwks_cache["keys"] = jwks
        _jwks_cache["expires"] = now + 3600  # Cache for 1 hour
        return jwks
    except Exception as e:
        logger.error(f"Failed to fetch JWKS from Auth0: {e}")
        return None


def validate_token(token: str) -> dict | None:
    """
    Validate a JWT token from Auth0.

    Returns the decoded token payload if valid, None otherwise.
    """
    if not settings.AUTH0_DOMAIN or not settings.AUTH0_API_IDENTIFIER:
        logger.warning("Auth0 not configured, skipping token validation")
        return None

    jwks = get_jwks()
    if not jwks:
        logger.warning("Failed to get JWKS")
        return None

    try:
        # Get the key ID from the token header
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # Find the matching key
        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = {
                    "kty": key["kty"],
                    "kid": key["kid"],
                    "use": key["use"],
                    "n": key["n"],
                    "e": key["e"],
                }
                break

        if not rsa_key:
            logger.warning(f"No matching key found for kid: {kid}")
            return None

        # Validate the token
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=settings.AUTH0_ALGORITHMS,
            audience=settings.AUTH0_API_IDENTIFIER,
            issuer=f"https://{settings.AUTH0_DOMAIN}/",
        )
        return payload

    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error validating token: {e}")
        return None


def get_bearer_token(request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None
