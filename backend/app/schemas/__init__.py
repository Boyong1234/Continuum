from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ConversationCreate(StrictModel):
    title: str = Field(default='新会话', min_length=1, max_length=200)


class ChatRequest(StrictModel):
    content: str = Field(min_length=1, max_length=100000)
    config_id: str = 'mock'
    request_id: str | None = Field(default=None, min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class MemoryCreate(StrictModel):
    memory_type: Literal['user_statement', 'model_inference', 'user_confirmed', 'outdated']
    content: str = Field(min_length=1, max_length=20000)
    confidence: float = Field(default=1, ge=0, le=1)
    source_message_ids: list[str] = Field(min_length=1, max_length=100)


class MemoryUpdate(StrictModel):
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    memory_type: Literal['user_statement', 'model_inference', 'user_confirmed', 'outdated'] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_message_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class MemoryLifecycleUpdate(StrictModel):
    expected_version: int = Field(ge=1, strict=True)
    reason: Literal['confirm','archive','reopen','conflict','retract','supersede','manual_update','time_update','topic_update']
    state: Literal['tentative','active','superseded','archived'] | None = None
    evidence_level: Literal['user_explicit','user_confirmed','model_inferred'] | None = None
    topic_key: str | None = Field(default=None, min_length=1, max_length=200)
    valid_from: str | None = None
    valid_to: str | None = None
    superseded_by: str | None = None
    confirmation_message_id: str | None = None


def conversation_dict(value):
    return {name: getattr(value, name) for name in ('id', 'title', 'created_at', 'updated_at')}


def message_dict(value):
    return {**{name: getattr(value, name) for name in ('id', 'conversation_id', 'role', 'content', 'model', 'created_at')}, 'metadata': value.metadata_json}
