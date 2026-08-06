"""Canonical IANA timezone handling for stable local-day identity."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError


@lru_cache(maxsize=1)
def _timezone_aliases() -> dict[str, str]:
    """Read the host tzdb link table so browser aliases share one day key."""

    aliases: dict[str, str] = {}
    for root in TZPATH:
        link_table = Path(root) / "tzdata.zi"
        if not link_table.is_file():
            continue
        for line in link_table.read_text(encoding="utf-8").splitlines():
            if not line.startswith("L "):
                continue
            _, canonical, alias = line.split()
            aliases[alias] = canonical
        break
    return aliases


def canonical_timezone(value: str) -> str:
    canonical = _timezone_aliases().get(value, value)
    try:
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError as error:
        raise ValueError("unknown IANA timezone") from error
    return canonical
