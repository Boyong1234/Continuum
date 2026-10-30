from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import String, Text, ForeignKey, JSON, Float, CheckConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200), default='新会话')
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow)


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (CheckConstraint("role IN ('user','assistant','system')"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id', ondelete='RESTRICT'), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow, index=True)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, default=dict)


class Memory(Base):
    __tablename__ = 'memories'
    __table_args__ = (
        CheckConstraint('confidence >= 0 AND confidence <= 1'),
        CheckConstraint("status IN ('active','archived')"),
        CheckConstraint("memory_type IN ('user_statement','model_inference','user_confirmed','outdated')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    memory_type: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String, default='active')


class MemorySource(Base):
    __tablename__ = 'memory_sources'
    memory_id: Mapped[str] = mapped_column(ForeignKey('memories.id', ondelete='RESTRICT'), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey('messages.id', ondelete='RESTRICT'), primary_key=True)


class MemoryRevision(Base):
    __tablename__ = 'memory_revisions'
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    memory_id: Mapped[str] = mapped_column(ForeignKey('memories.id', ondelete='RESTRICT'), index=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    snapshot: Mapped[dict] = mapped_column(JSON)
