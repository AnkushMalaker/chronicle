"""Shared pytest fixtures and test environment defaults.

Several backend modules (notably ``advanced_omi_backend.auth``) validate that
required secrets are configured at *import* time. In CI there is no ``.env``
file, so importing the app during test collection would raise
``ValueError: <VAR> is not set``. We provide deterministic test defaults here so
collection succeeds without depending on a developer's local ``.env``.

``setdefault`` is used so a real environment (CI secrets or a local ``.env``
already exported) always wins over these placeholders.
"""

import os

# Import-time required secrets (see advanced_omi_backend.auth).
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
