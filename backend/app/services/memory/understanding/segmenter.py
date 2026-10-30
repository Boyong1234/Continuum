"""Deterministic, bounded snapshots; silence alone never implies a topic break."""
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import AppError
from app.services.memory.jobs import source_fingerprint


@dataclass(frozen=True)
class SourceMessage:
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: str
    metadata: dict = field(default_factory=dict, compare=True)


@dataclass(frozen=True)
class ConversationSegment:
    primary: tuple[SourceMessage, ...]
    context: tuple[SourceMessage, ...]
    fingerprint: str
    policy_version: str = 'segment-v1'

    @property
    def messages(self):
        return self.context + self.primary


@dataclass(frozen=True)
class RecentInteractionContext:
    messages: tuple[SourceMessage, ...]
    last_interaction_at: str | None
    # Derived topics/summaries must carry sources; no automatic summary in B1.
    open_topics: tuple[str, ...] = ()


class SemanticSegmentGrouper(Protocol):
    def related(self, segments: tuple[ConversationSegment, ...]) -> list[tuple[str, ...]]:
        """Return fingerprint groups for interpretation, never mutate base slices."""
        ...


class ContinuityContextSource(Protocol):
    def recent(self, conversation_id: str) -> RecentInteractionContext: ...


class ConversationSegmenter:
    def __init__(self, *, max_messages=40, max_bytes=16000, overlap_messages=2):
        if not 2 <= max_messages <= 100 or max_bytes < 128 or not 0 <= overlap_messages < max_messages:
            raise ValueError('Invalid segment budget')
        self.max_messages, self.max_bytes, self.overlap = max_messages, max_bytes, overlap_messages

    def build(self, messages):
        rows=sorted(messages,key=lambda m:(m.created_at,m.id))
        if len({m.id for m in rows})!=len(rows) or len({m.conversation_id for m in rows})>1:
            raise ValueError('Expected unique messages from one conversation')
        for m in rows:
            from app.services.memory.lifecycle import normalized_time
            normalized_time(m.created_at)
            if m.role not in ('user','assistant','system'):
                raise ValueError('Invalid source role')
            if len(m.content.encode())+32>self.max_bytes:
                raise AppError('segment_budget','单条消息超出提取预算；原文未修改，请提高预算或单独处理。',422)
        result=[]; start=0
        while start<len(rows):
            context=list(rows[max(0,start-self.overlap):start])
            # Leave room for at least the first primary message.
            while context and (len(context)+1>self.max_messages or sum(len(m.content.encode())+32 for m in context+[rows[start]])>self.max_bytes):
                context.pop(0)
            primary=[]; size=sum(len(m.content.encode())+32 for m in context)
            while start+len(primary)<len(rows):
                m=rows[start+len(primary)]
                switch=m.role=='user' and m.content.lstrip().startswith(('换个话题','另外一件事','另一个问题'))
                if primary and (switch or len(context)+len(primary)>=self.max_messages or size+len(m.content.encode())+32>self.max_bytes):break
                primary.append(m); size+=len(m.content.encode())+32
            all_rows=context+primary
            # Exactly the queue's canonical immutable source fingerprint.
            fp=source_fingerprint([tuple(getattr(m,k) for k in ('id','conversation_id','role','content','created_at')) for m in all_rows])
            if any(m.role=='user' for m in all_rows):
                result.append(ConversationSegment(tuple(primary),tuple(context),fp))
            start+=len(primary)
        return result

    def recent(self,messages):
        segments=self.build(messages)
        values=segments[-1].messages if segments else ()
        return RecentInteractionContext(values,values[-1].created_at if values else None)
