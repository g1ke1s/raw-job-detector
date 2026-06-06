import logging
from datetime import datetime
from app.db.session import AsyncSessionLocal
from app.db.models import EventLog

log = logging.getLogger(__name__)


async def log_event(event: str, detail: str = "", level: str = "INFO") -> None:
    try:
        async with AsyncSessionLocal() as s:
            s.add(EventLog(ts=datetime.utcnow(), level=level, event=event, detail=detail[:1000]))
            await s.commit()
    except Exception as e:
        log.warning("log_event failed: %s", e)
