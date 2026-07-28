"""Locating the Chronicle checkout.

Setup code needs the repository root to find ``config/config.yml`` and each
service's ``.env``. It cannot be derived by counting parent directories from
``__file__``: this package is installed into a virtualenv, so its own path says
nothing about where the checkout is. Marker files are searched for instead.

``chronicle_client`` deliberately keeps its own copy of this lookup rather than
depending on this package. Its fallback differs — it ships inside the relay
container, where there is no checkout at all and configuration comes from the
environment, so it must degrade quietly. Setup only ever runs inside a checkout,
so here a missing root is a hard error.
"""

import os
from pathlib import Path

# Files that only exist together at the repository root.
_ROOT_MARKERS = ("discovery.py", "wizard.py", "setup-requirements.txt")


def looks_like_repo_root(path: Path) -> bool:
    return all((path / marker).is_file() for marker in _ROOT_MARKERS)


def find_repo_root(start: Path = None) -> Path:
    """Return the Chronicle checkout root.

    Honours ``CHRONICLE_REPO_ROOT``, then walks up from ``start`` (default: the
    working directory). Setup commands run from the repository root, so the
    first candidate is normally the answer.

    Raises:
        RuntimeError: if no checkout is found — better than silently writing a
            ``.env`` into an unrelated directory.
    """
    override = os.getenv("CHRONICLE_REPO_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if looks_like_repo_root(candidate):
            return candidate
        raise RuntimeError(
            f"CHRONICLE_REPO_ROOT={candidate} is not a Chronicle checkout "
            f"(expected {', '.join(_ROOT_MARKERS)})"
        )

    begin = Path(start).resolve() if start else Path.cwd().resolve()
    # `Path.parents` excludes the path itself, so include the start directory.
    for candidate in (begin, *begin.parents):
        if looks_like_repo_root(candidate):
            return candidate

    raise RuntimeError(
        f"No Chronicle checkout found at or above {begin}. Run setup commands "
        "from the repository root, or set CHRONICLE_REPO_ROOT."
    )
