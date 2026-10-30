"""Untrusted extractor output. Never persist a raw model dictionary as a fact."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

from app.services.memory.lifecycle import normalized_time


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)


class ChannelOrigin(StrictModel):
    channel: str = Field(default='web',min_length=1,max_length=80)
    external_message_id: str | None = Field(default=None,max_length=200)
    received_at: str | None = None
    source_conversation: str | None = Field(default=None,max_length=200)
    source_system: str = Field(default='continuum',max_length=100)
    # User/assistant roles always come from Message, including future imports.


class EvidenceSpan(StrictModel):
    message_id: str = Field(min_length=1,max_length=100)
    role: Literal['user','assistant']
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1,max_length=16000)


class Candidate(StrictModel):
    candidate_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,80}$')
    content: str = Field(min_length=1,max_length=2000)
    evidence: list[EvidenceSpan] = Field(min_length=1,max_length=40)
    evidence_level: Literal['user_explicit','user_confirmed','model_inferred']
    confidence: float = Field(ge=0,le=1)
    uncertainty: Literal['none','ambiguous','hypothesis']
    valid_from: str | None = None
    valid_to: str | None = None
    effect: Literal['new','supplement','replace','correct','conflict'] = 'new'
    target_memory_id: str | None = Field(default=None,max_length=100)

    @model_validator(mode='after')
    def bounds(self):
        from app.core.config import AppError
        try:
            self.valid_from=normalized_time(self.valid_from)
            self.valid_to=normalized_time(self.valid_to)
        except AppError:
            raise ValueError('Invalid UTC bounds') from None
        if self.valid_from and self.valid_to and self.valid_to<=self.valid_from:
            raise ValueError('Invalid validity bounds')
        return self


class SemanticCandidate(Candidate):
    memory_kind: Literal['fact','preference','goal','viewpoint','project','conclusion'] = 'fact'
    topic_key: str = Field(min_length=1,max_length=200)


class DiscussionPoint(StrictModel):
    # Explicitly separate user views from assistant hypotheses and agreed results.
    perspective: Literal['user_view','assistant_interpretation','agreed_conclusion','open_question']
    content: str = Field(min_length=1,max_length=2000)
    evidence: list[EvidenceSpan] = Field(min_length=1,max_length=20)


class EpisodicCandidate(Candidate):
    title: str = Field(min_length=1,max_length=160)
    importance: float = Field(ge=0,le=1)
    discussion: list[DiscussionPoint] = Field(default_factory=list,max_length=20)
    continuation: str | None = Field(default=None,max_length=1000)
    # Conditions are descriptive, never schedules or permission to send messages.
    continuation_condition: str | None = Field(default=None,max_length=1000)
    linked_candidate_ids: list[str] = Field(default_factory=list,max_length=20)


class ExtractionResult(StrictModel):
    schema_version: Literal[1] = 1
    extractor_version: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}$')
    episodic: list[EpisodicCandidate] = Field(default_factory=list,max_length=12)
    semantic: list[SemanticCandidate] = Field(default_factory=list,max_length=12)

    @field_validator('schema_version',mode='before')
    @classmethod
    def exact_version(cls,value):
        if type(value) is not int:raise ValueError('Version must be integer')
        return value

    @model_validator(mode='after')
    def unique_ids(self):
        ids=[c.candidate_id for c in self.episodic+self.semantic]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate candidate IDs')
        semantic={c.candidate_id for c in self.semantic}
        if any(set(e.linked_candidate_ids)-semantic for e in self.episodic):raise ValueError('Unknown linked candidate')
        return self
