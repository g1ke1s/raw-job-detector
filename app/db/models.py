from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Float,
    DateTime, Boolean, JSON, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class AllMessage(Base):
    __tablename__ = "all_messages"
    __table_args__ = (UniqueConstraint("source", "message_id"),)

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False)
    message_id = Column(String(128), nullable=False)
    raw_text = Column(Text)
    url = Column(Text)
    scraped_at = Column(DateTime, default=datetime.utcnow)


class SeenJob(Base):
    __tablename__ = "seen_jobs"

    id = Column(Integer, primary_key=True)
    content_fingerprint = Column(String(256), unique=True, nullable=False)
    source = Column(String(32))
    first_seen = Column(DateTime, default=datetime.utcnow)


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False)
    external_id = Column(String(128))
    title = Column(String(256))
    company = Column(String(256))
    url = Column(Text)
    description = Column(Text)
    rule_score = Column(Float, default=0.0)
    low_conf = Column(Boolean, default=False)
    status = Column(String(32), default="waiting")
    telegram_message_id = Column(BigInteger)
    recruiter_handle = Column(String(128))
    cover_text = Column(Text)
    night_run = Column(Boolean, default=False)
    seniority = Column(String(32), nullable=True)
    job_json = Column(JSON, nullable=True)
    fit_assessment_json = Column(JSON, nullable=True)
    cv_tailor_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    extra = Column(JSON, default=dict)


class Decision(Base):
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    verdict = Column(String(16))
    reason = Column(String(64))
    decided_at = Column(DateTime, default=datetime.utcnow)


class RunLog(Base):
    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    status = Column(String(16), default="running")
    scraped = Column(Integer, default=0)
    filtered = Column(Integer, default=0)
    enqueued = Column(Integer, default=0)
    llm_calls = Column(Integer, default=0)
    is_night = Column(Boolean, default=False)
    error_msg = Column(Text)


class EventLog(Base):
    __tablename__ = "event_logs"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow)
    level = Column(String(8), default="INFO")
    event = Column(String(64))
    detail = Column(Text)


class FindSession(Base):
    """Tracks an active /find or /findall browse session.
    Closed when all jobs are decided or user runs /find again.
    Dispatcher checks closed_at IS NULL before auto-sending cards.
    """
    __tablename__ = "find_sessions"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    match_ids = Column(JSON, nullable=False)  # ordered list[int] — display order
    decisions = Column(JSON, default=dict)    # {str(match_id): icon}
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)  # NULL = active


class AppConfig(Base):
    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, default=True, nullable=False)
    raw_cv_text = Column(Text)
    structured_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)