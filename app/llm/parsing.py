import json
import re
from typing import Any, Optional


def safe_json_parse(raw: str) -> Optional[Any]:
    """Strip markdown fences, extract first JSON object/array, parse safely."""
    s = (raw or "").strip()
    # strip ```json ... ``` fences
    s = re.sub(r"^```[a-z]*\n?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"```$", "", s).strip()
    # find outermost { } or [ ]
    for opener, closer in [('{', '}'), ('[', ']')]:
        start = s.find(opener)
        end = s.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None
