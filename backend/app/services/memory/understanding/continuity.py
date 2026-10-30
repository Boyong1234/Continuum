"""Read-only continuity interfaces reserved for P2-C; no prompt injection here."""
import json
import re
from dataclasses import dataclass
from typing import Protocol
from sqlalchemy import text
from app.core.config import AppError
from app.services.memory.understanding.segmenter import RecentInteractionContext


@dataclass(frozen=True)
class ContinuityBudget:
    recent: int = 6000
    episodes: int = 1000
    semantic: int = 1000
    open_topics: int = 300


@dataclass(frozen=True)
class ContinuityBundle:
    recent: RecentInteractionContext
    episodes: tuple[dict, ...] = ()
    semantic: tuple[dict, ...] = ()
    # Descriptive conditions only, not permission or a trigger to send messages.
    open_topics: tuple[dict, ...] = ()


class ContinuityRetriever(Protocol):
    def retrieve(self,query: str,*,as_of: str,budget: ContinuityBudget) -> ContinuityBundle: ...


def event_labels(value):
    """Explicit named projects only; generic research/experiment is not identity."""
    labels=re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}(?:项目|课题)|[\u4e00-\u9fff]{2,6}(?:项目|课题)',value)
    return {label for label in labels if label not in ('科研项目','研究项目','这个项目','那个项目','实验项目','我的项目')}


class EpisodeStore:
    def __init__(self,session):
        self.session=session
        if session.execute(text('SELECT version FROM schema_info')).scalar_one()<5:
            raise AppError('migration_required','事件记忆需要显式迁移的 v5 副本。',409)

    def get(self,episode_id):
        row=self.session.execute(text('SELECT * FROM episodes WHERE id=:id'),dict(id=episode_id)).mappings().first()
        if row is None:raise AppError('not_found','事件记忆不存在。',404)
        result=dict(row);result['candidate']=json.loads(result.pop('candidate_json'))
        result['source_message_ids']=list(self.session.execute(text('SELECT message_id FROM episode_sources WHERE episode_id=:id ORDER BY message_id'),dict(id=episode_id)).scalars())
        result['linked_memory_ids']=list(self.session.execute(text('SELECT memory_id FROM episode_memory_links WHERE episode_id=:id ORDER BY memory_id'),dict(id=episode_id)).scalars())
        result['superseded_by']=list(self.session.execute(text('SELECT id FROM episodes WHERE supersedes=:id ORDER BY created_at,id'),dict(id=episode_id)).scalars())
        if result['superseded_by']:result['state']='superseded'
        version=self.session.execute(text('SELECT version FROM schema_info')).scalar_one()
        if version>=6 and self.session.execute(text("SELECT target_id FROM memory_suppressions WHERE kind='episodic' AND target_id=:id"),dict(id=episode_id)).first():result['state']='archived'
        return result

    def search(self,terms,limit=10, *, include_discussion=False,candidate_ids=None):
        if not terms:return []
        exclusion=" AND NOT EXISTS(SELECT 1 FROM episode_sources es JOIN memory_exclusions x ON x.message_id=es.message_id WHERE es.episode_id=e.id) AND NOT EXISTS(SELECT 1 FROM memory_suppressions b WHERE b.kind='episodic' AND b.target_id=e.id)" if self.session.execute(text('SELECT version FROM schema_info')).scalar_one()>=6 else ''
        clauses=[]
        for i in range(len(terms)):
            clause=f"(e.title LIKE :q{i} ESCAPE char(92) OR e.content LIKE :q{i} ESCAPE char(92)"
            if include_discussion:
                clause+=f" OR EXISTS(SELECT 1 FROM json_each(e.candidate_json,'$.discussion') d WHERE json_extract(d.value,'$.content') LIKE :q{i} ESCAPE char(92))"
            clauses.append(clause+')')
        params={f'q{i}':'%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for i,term in enumerate(terms)}
        bounded=''
        if candidate_ids is not None:
            if not candidate_ids:return []
            bounded=' AND e.id IN ('+','.join(':candidate'+str(i) for i in range(len(candidate_ids)))+')'
            params.update({'candidate'+str(i):id for i,id in enumerate(candidate_ids)})
        statement=text("SELECT id FROM episodes e WHERE e.state='active' AND NOT EXISTS(SELECT 1 FROM episodes n WHERE n.supersedes=e.id)"+bounded+exclusion+' AND ('+' OR '.join(clauses)+') ORDER BY e.ended_at DESC,e.id LIMIT :limit')
        return [self.get(id) for id in self.session.execute(statement,{**params,'limit':limit}).scalars()]

    def timeline(self,episode_id,*,before=None,limit=3):
        """A bounded read-only view of existing evidence, not a generated life story."""
        seed=self.get(episode_id)
        from app.models import Message
        from app.services.dialogue import origin_of
        labels=event_labels(seed['title'])
        if not labels:return [seed]
        ids=list(self.session.execute(text('SELECT id FROM episodes WHERE conversation_id=:cid ORDER BY started_at DESC,id LIMIT 64'),dict(cid=seed['conversation_id'])).scalars())
        events=[]
        for id in dict.fromkeys([episode_id]+ids):
            e=self.get(id)
            if e['state']!='active' or (before and e['ended_at']>=before):continue
            if not labels & event_labels(e['title']):continue
            if self.session.execute(text('SELECT version FROM schema_info')).scalar_one()>=6 and self.session.execute(text('SELECT 1 FROM episode_sources es JOIN memory_exclusions x ON x.message_id=es.message_id WHERE es.episode_id=:id LIMIT 1'),dict(id=id)).first():continue
            direct_ids={id for id in e['source_message_ids'] if (m:=self.session.get(Message,id)) is not None and origin_of(m).assertable}
            user_quotes=[span['quote'] for span in e['candidate']['evidence'] if span['message_id'] in direct_ids]
            if not any(label in quote for label in labels for quote in user_quotes):continue
            continuation=e['candidate'].get('continuation')
            e['progress_evidence_ids']=[span['message_id'] for span in e['candidate']['evidence']
                if span['message_id'] in direct_ids and continuation and continuation in span['quote']
                and re.search(r'继续|后续|进展|后来|已经|改为|不再',continuation)]
            events.append(e)
        events.sort(key=lambda e:(e['ended_at'],e['id']))
        linked=[]
        for e in events:
            earlier=[old for old in events if old['ended_at']<e['started_at']]
            if e['progress_evidence_ids'] and earlier:
                e['previous_episode_ids']=[earlier[-1]['id']]
                linked.extend([earlier[-1],e])
        linked={e['id']:e for e in linked}
        if episode_id not in linked:return [seed]
        return sorted(linked.values(),key=lambda e:(e['ended_at'],e['id']))[-limit:]
