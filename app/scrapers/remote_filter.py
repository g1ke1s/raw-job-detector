"""
Rule-based filter for remote job boards. Zero LLM.
Checks: role is in-field AND (job is remote or location-free).
"""
import re
from app.scrapers.structured_filter import title_is_relevant

_LOCATION_BLOCK = re.compile(
    r"\b(russia|china|india|brazil|nigeria|pakistan|bangladesh|"
    r"us only|usa only|united states only|canada only|eu only|"
    r"россия|китай)\b",
    re.IGNORECASE,
)

_REMOTE_OK = re.compile(
    r"\b(remote|anywhere|worldwide|global|distributed|fully remote|"
    r"work from home|wfh|удалённ|удаленн|全リモート)\b",
    re.IGNORECASE,
)


def job_passes_remote_filter(title: str, location: str = "", tags: list[str] = None) -> bool:
    """
    Returns True if:
    - title is in-field (via structured_filter)
    - AND location is either remote/empty or not geo-blocked
    """
    if not title_is_relevant(title):
        return False

    loc = (location or "").strip()

    # empty location = remote-friendly on most boards
    if not loc:
        return True

    loc_lower = loc.lower()
    # explicit remote signals always pass
    if _REMOTE_OK.search(loc_lower):
        return True

    # geo-block check
    if _LOCATION_BLOCK.search(loc_lower):
        return False

    # tags: if "remote" in tags, pass
    if tags and any("remote" in str(t).lower() for t in tags):
        return True

    return True  # default pass — let dispatcher handle edge cases
