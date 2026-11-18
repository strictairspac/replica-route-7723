from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text

from src.config.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    email = Column(String(120), unique=True, nullable=False)
    password = Column(String(300), nullable=False)
    role = Column(String(20), nullable=False)


class Build(Base):
    """One online unique-index build with its frozen snapshot and event log."""
    __tablename__ = 'index_builds'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    table_name = Column(String(80), nullable=False)
    index_name = Column(String(80), nullable=False)
    key_column = Column(String(80), nullable=False)
    policy = Column(String(20), nullable=False)
    batch_size = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default='building')
    initial_state = Column(JSON, nullable=False)
    events = Column(JSON, nullable=False)
    state = Column(JSON, nullable=False)
    summary = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BuildJob(Base):
    """Background deterministic verification of a build's event log."""
    __tablename__ = 'build_jobs'
    id = Column(Integer, primary_key=True)
    build_id = Column(Integer, ForeignKey('index_builds.id'), nullable=False)
    status = Column(String(20), nullable=False, default='queued')
    expected_checksum = Column(String(80), nullable=True)
    actual_checksum = Column(String(80), nullable=True)
    event_count = Column(Integer, nullable=False, default=0)
    artifact = Column(String(300), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id = Column(Integer, primary_key=True)
    actor = Column(String(120), nullable=False)
    action = Column(String(160), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
