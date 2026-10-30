"""Provider-independent seam for future local hybrid retrieval.

Only the lexical SQLite implementation is shipped. No embedding requests are made.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
import re
import json
import sqlite3
from sqlalchemy import text

from app.services.memory import MemoryManager

FACT_TYPES = ('user_statement', 'user_confirmed')


@dataclass(frozen=True)
class RetrievalRequest:
    text: str
    limit: int = 10
    memory_types: tuple[str, ...] = FACT_TYPES
    chat_context: bool = False
    exclude_content: str = ""
    before: str | None = None

    def __post_init__(self):
        if not 1 <= self.limit <= 100:
            raise ValueError('Retrieval limit must be between 1 and 100')
        if not self.memory_types or not set(self.memory_types).issubset(FACT_TYPES):
            raise ValueError('Conversation retrieval only accepts explicit or confirmed facts')


@dataclass(frozen=True)
class RetrievalHit:
    memory: dict
    channels: tuple[str, ...]
    score: float | None = None


class MemoryRetriever(Protocol):
    def retrieve(self, request: RetrievalRequest) -> list[RetrievalHit]:
        """Return sourced history with explicit roles; Core facts stay lifecycle-filtered."""
        ...


class SQLiteFTS5Retriever:
    def __init__(self, sessions):
        self.sessions = sessions

    def retrieve(self, request: RetrievalRequest) -> list[RetrievalHit]:
        if not request.text.strip():
            return []
        with self.sessions() as session:
            memories = MemoryManager(session).search_memories(request.text, limit=request.limit, memory_types=request.memory_types)
            return [RetrievalHit(memory=m, channels=('sqlite_lexical',)) for m in memories]


def recall_terms(value):
    """Literal Chinese bigrams and word tokens, not semantic query expansion."""
    value=re.sub(r'你还记得|还记得|我之前|我以前|我们之前|我们上次|跟你说过|跟你提过|那个|的事情|是不是|的时候|吗|什么', ' ',value)
    terms=[]
    for word in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+',value.casefold()):
        terms.extend([word] if word.isascii() else [word[i:i+2] for i in range(len(word)-1)])
    return list(dict.fromkeys(terms))[:32]


# Function words and dialogue scaffolding are not evidence of shared subject matter.
_TOPIC_NOISE = re.compile(r'chatgpt|deeptalk|gpt|我们|咱们|你们|之前|以前|上次|过去|当时|今天|现在|刚刚|那个|这个|一个|一些|这些|那些|这件事情|这件事|这事|的时候|最后|后来|最初|解决|做完|完成|进展|明确|确定|为什么|怎么|如何|哪些|什么|是否|是不是|有没有|还记得|回忆|回顾|聊过|讲过|说过|提过|讨论过|讨论|记得|讲|聊|说|提|关于|有点|好像|终于|还是|仍然|担心|问题|事情|一下|只要|我|你|它|跟|和|与|在|是|做|的|了|又|把|时|呢|吗|哦|嗯|啊',re.I)


def history_clause(value):
    clauses=re.split(r'[，,。；;！？?\n]',value)
    references=[c for c in clauses if re.search(r'之前|以前|上次|过去|当时|还记得|回忆|回顾',c)]
    return '，'.join(references) if references else value


def topic_terms(value):
    # Small documented aliases, not semantic inference or user-specific vocabulary.
    value=history_clause(value)
    if '计网' in value:value+=' 计算机网络'
    if '科研方向' in value:value+=' 研究方向'
    value=_TOPIC_NOISE.sub(' ',value)
    terms=[]
    for word in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+',value.casefold()):
        if len(word)<2:continue
        if not word.isascii() and len(word)>2:terms.extend(word[i:i+2] for i in range(len(word)-1))
        terms.append(word)
    return sorted(set(terms),key=lambda t:(-len(t),t))[:32]


class EpisodeLexicalIndex:
    """Disposable FTS5 tokens/IDs, rebuilt from Archive-derived immutable Episodes."""
    def __init__(self):
        self.connection=sqlite3.connect(':memory:',check_same_thread=False)
        self.connection.execute('CREATE VIRTUAL TABLE lookup USING fts5(id UNINDEXED, terms)')
        self.cursor=0

    @staticmethod
    def tokens(value):
        tokens=[]
        for word in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+',value.casefold()):
            tokens.extend([word] if word.isascii() else [word[i:i+2] for i in range(len(word)-1)])
        return list(dict.fromkeys(tokens))

    def candidates(self,session,terms):
        # Initial build is local only; subsequent queries read just new rows.
        rows=session.execute(text('SELECT rowid,id,title,content,candidate_json FROM episodes WHERE rowid>:cursor ORDER BY rowid'),dict(cursor=self.cursor))
        for row in rows:
            discussion=' '.join(p['content'] for p in json.loads(row.candidate_json).get('discussion',[]))
            tokens=' '.join(self.tokens(row.title+' '+row.content+' '+discussion))
            self.connection.execute('INSERT INTO lookup(id,terms) VALUES (?,?)',(row.id,tokens))
            self.cursor=row.rowid
        self.connection.commit()
        tokens=self.tokens(' '.join(terms))
        if not tokens:return []
        query=' OR '.join('"'+token+'"' for token in tokens)
        return [row[0] for row in self.connection.execute('SELECT id FROM lookup WHERE lookup MATCH ? ORDER BY bm25(lookup),id LIMIT 64',(query,))]


class LocalHistoryRetriever:
    """Episode navigation, sourced Core facts, then small Archive excerpts.

    The existing compiler still applies Recent-first budgeting and state filters.
    No persistent index or model call is introduced.
    """
    def __init__(self,sessions):self.sessions=sessions;self.episode_index=EpisodeLexicalIndex()

    def archive_hit(self,row,score,terms=()):
        return RetrievalHit(dict(id=row['id'],kind='archive',status='active',evidence_level='archive_record',
            content=matched_excerpt(row['content'],terms,600),source_role=row['role'],origin=json.loads(row['metadata']).get('origin','original_record'),
            created_at=row['created_at'],source_message_ids=[row['id']],attachments_missing=json.loads(row['metadata']).get('attachments_missing',False),
            perspective='原始发言记录，不自动代表用户事实、认可或当前状态'),('archive_literal',),score)

    def retrieve(self,request):
        from app.services.memory.understanding.continuity import EpisodeStore
        terms=topic_terms(request.text) if request.chat_context else recall_terms(request.text)
        if not terms:return []
        def score(content):return sum(term in content.casefold() for term in terms)
        with self.sessions() as s:
            version=s.execute(text('SELECT version FROM schema_info')).scalar_one()
            candidate_ids=self.episode_index.candidates(s,terms) if version>=5 else []
            episodes=EpisodeStore(s).search(terms,30,include_discussion=request.chat_context,candidate_ids=candidate_ids) if version>=5 else []
            episodes=[e for e in episodes if not request.before or e['ended_at']<request.before]
            episodes.sort(key=lambda e:score(e['title']+' '+e['content']+(' '.join(p['content'] for p in e['candidate'].get('discussion',[])) if request.chat_context else '')),reverse=True)
            hits=[]
            for episode in (episodes if request.chat_context else episodes[:3]):
                hits.append(RetrievalHit(dict(id=episode['id'],kind='episode',status='active',
                    evidence_level='source_backed_discussion',content=episode['content'],title=episode['title'],
                    time_range=[episode['started_at'],episode['ended_at']],
                    source_message_ids=episode['source_message_ids'],discussion=episode['candidate'].get('discussion',[]),
                    perspective='历史讨论摘录，不等于当前事实；助手观点不等于用户认可'),('episode_metadata',),score(episode['title']+' '+episode['content'])))
                if score(episode['content'])<2:
                    sources=s.execute(text('SELECT m.id,m.content,m.role,m.created_at,m.metadata FROM messages m JOIN episode_sources es ON es.message_id=m.id WHERE es.episode_id=:id'),dict(id=episode['id'])).mappings().all()
                    relevant=[row for row in sources if row['content']!=episode['content'] and score(row['content'])]
                    if relevant:
                        best=max(relevant,key=lambda row:score(row['content']))
                        hits.append(self.archive_hit(best,score(best['content']),terms if request.chat_context else ()))
            memories=MemoryManager(s).search_memories(limit=30,memory_types=request.memory_types,query_terms=terms)
            memories.sort(key=lambda m:score(m['content']),reverse=True)
            hits.extend(RetrievalHit(m,('sqlite_lexical',),score(m['content'])) for m in (memories if request.chat_context else memories[:3]))
            # Include dated originals so an old Episode cannot hide a later correction.
            clauses=' OR '.join(f'm.content LIKE :q{i} ESCAPE \'\\\'' for i in range(len(terms)))
            excluded=" AND NOT EXISTS(SELECT 1 FROM memory_exclusions x WHERE x.message_id=m.id)" if version>=6 else ''
            params={f'q{i}':'%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for i,term in enumerate(terms)}
            # Use the existing created_at index to bound raw-history work BEFORE LIKE.
            rows=s.execute(text("WITH recent AS MATERIALIZED (SELECT id,content,role,created_at,metadata FROM messages WHERE (:before IS NULL OR created_at<:before) ORDER BY created_at DESC LIMIT 512) SELECT m.id,m.content,m.role,m.created_at,m.metadata FROM recent m WHERE m.role IN ('user','assistant') AND m.content<>:current"+excluded+' AND ('+clauses+') ORDER BY m.created_at DESC,m.id DESC LIMIT 80'),{**params,'current':request.exclude_content or request.text,'before':request.before}).mappings().all()
            rows=sorted(rows,key=lambda m:score(m['content']),reverse=True)
            for row in (rows if request.chat_context else rows[:3]):
                hits.append(self.archive_hit(row,score(row['content']),terms if request.chat_context else ()))
            from app.models import Message
            source_ids={id for hit in hits for id in hit.memory.get('source_message_ids',[])}
            private={id:row.created_at for id in source_ids if (row:=s.get(Message,id)) is not None and row.metadata_json.get('cloud_export_allowed') is False}
            for hit in hits:
                dates = [private[id] for id in hit.memory.get('source_message_ids',[]) if id in private]
                if dates:
                    hit.memory.update(local_only=True,source_times=dates)
            return self.chat_fragments(s, hits, terms, request.limit, before=request.before) if request.chat_context else hits[:request.limit]


    def chat_fragments(self, session, hits, terms, limit, *, before=None):
        from app.models import Message
        result=[]
        def body(memory):return memory.get('title','')+' '+memory['content']+' '.join(p['content'] for p in memory.get('discussion',[]))
        # The first query subject actually present in this candidate set is the anchor.
        anchor=next((t for t in terms if any(t in body(h.memory).casefold() for h in hits)),None)
        if anchor is None:return []
        def relevance(content):return sum((3 if t==anchor else 1)*len(t) for t in terms if t in content.casefold())
        # Keep sourced discussion points, never generate a summary or expand originals.
        for hit in hits:
            memory=hit.memory
            if anchor not in body(memory).casefold():continue
            subject_score=relevance(body(memory))+2*relevance(memory.get('title',''))
            if memory.get('kind')=='episode':
                from app.services.memory.understanding.continuity import EpisodeStore
                events=EpisodeStore(session).timeline(memory['id'],before=before,limit=2)
                if len(events)>1:
                    ids=list(dict.fromkeys(id for e in events for id in e['source_message_ids']))
                    sources=[session.get(Message,id) for id in ids]
                    # A compact timeline only for exact user records. Mixed discussion
                    # keeps its individual perspective-marked fragments below.
                    if all(any(span['role']=='user' and span['quote']==e['content'] for span in e['candidate']['evidence']) for e in events):
                        packet=dict(id='timeline:'+events[-1]['id'],kind='episode',
                            content='\n'.join(e['ended_at'][:10]+' 用户原话：'+matched_excerpt(e['content'],terms,120) for e in events),
                            episode_ids=[e['id'] for e in events],source_message_ids=ids,
                            sources=[dict(id=m.id,role=m.role,at=m.created_at) for m in sources],
                            perspective='dated_user_records_not_current_fact',
                            temporal_role='historical_reference_not_current_fact',
                            local_only=any(m.metadata_json.get('cloud_export_allowed') is False for m in sources),
                            chat_export_eligible=all(m.metadata_json.get('cloud_export_allowed') is not False and (m.metadata_json.get('channel')=='qq_onebot_v11' or m.metadata_json.get('import_contract')=='deeptalk-text-reviewed-v1') for m in sources))
                        result.append(RetrievalHit(packet,('episode_continuity',),subject_score+20))
                        continue
            points=memory.get('discussion') or [dict(content=memory['content'], perspective=memory.get('perspective','sourced_record'), evidence=[])]
            if memory.get('kind')=='episode' and len(points)<=4:
                ids=memory['source_message_ids']
                sources=[session.get(Message,id) for id in ids]
                if ids and all(row is not None and (not before or row.created_at<before) for row in sources):
                    packet=dict(id=memory['id'],kind='episode',status=memory.get('status','active'),
                                content=memory['content'],title=memory.get('title',''),
                                discussion=[dict(content=p['content'],perspective=p['perspective'],source_message_ids=list(dict.fromkeys(e['message_id'] for e in p.get('evidence',[])))) for p in points],
                                source_message_ids=ids,sources=[dict(id=row.id,role=row.role,at=row.created_at) for row in sources],
                                time_range=memory.get('time_range'),temporal_role='historical_reference_not_current_fact',
                                user_confirmed=False,evidence_level=memory.get('evidence_level','source_backed_discussion'),
                                local_only=bool(memory.get('local_only')),
                                chat_export_eligible=all(row.metadata_json.get('cloud_export_allowed') is not False and (row.metadata_json.get('import_contract')=='deeptalk-text-reviewed-v1' or row.metadata_json.get('channel')=='qq_onebot_v11') for row in sources))
                    visible={k:v for k,v in packet.items() if k not in ('local_only','chat_export_eligible')}
                    if len(json.dumps(visible,ensure_ascii=False).encode('utf8'))<=1800:
                        result.append(RetrievalHit(packet,hit.channels,subject_score))
                        continue
            points=sorted(points,key=lambda p:relevance(p['content']),reverse=True)
            for index, point in enumerate(points[:3]):
                ids=list(dict.fromkeys(e['message_id'] for e in point.get('evidence',[]))) or memory.get('source_message_ids',[])
                sources=[session.get(Message,id) for id in ids]
                if not ids or any(row is None or (before and row.created_at>=before) for row in sources):continue
                eligible=all(row.metadata_json.get('cloud_export_allowed') is not False and (row.metadata_json.get('import_contract')=='deeptalk-text-reviewed-v1' or row.metadata_json.get('channel')=='qq_onebot_v11') for row in sources)
                private=bool(memory.get('local_only')) or any(row.metadata_json.get('cloud_export_allowed') is False for row in sources)
                item=dict(id=memory['id']+':'+str(index),kind=memory.get('kind','core'),status=memory.get('status','active'),
                          content=matched_excerpt(point['content'],terms,240),perspective=point['perspective'],user_confirmed=point['perspective']=='agreed_conclusion' or memory.get('memory_type')=='user_confirmed',
                          source_message_ids=ids,sources=[dict(id=row.id,role=row.role,at=row.created_at) for row in sources],
                          temporal_role='historical_reference_not_current_fact',local_only=private,chat_export_eligible=eligible)
                if memory.get('evidence_level'):item['evidence_level']=memory['evidence_level']
                if memory.get('time_range'):item['time_range']=memory['time_range']
                if memory.get('attachments_missing'):item['attachments_missing']=True
                if len(point['content'])>240:item['excerpt_truncated']=True
                score=subject_score if memory.get('kind')=='episode' else relevance(item['content'])
                if score:result.append(RetrievalHit(item,hit.channels,score))
        from app.services.dialogue import origin_of
        from types import SimpleNamespace
        episode_end=max(((m.memory.get('time_range') or [None,None])[1] or '' for m in result if m.memory.get('kind')=='episode'),default='')
        for hit in result:
            m=hit.memory
            if m['kind']=='archive' and m['sources'][0]['role']=='user' and episode_end and m['sources'][0]['at']>episode_end:
                if origin_of(SimpleNamespace(role='user',content=m['content'])).assertable and re.search(r'纠正|改正|记错|不再|不是.{0,20}而是',m['content']):m['later_user_record']=True
        order={'episode':0,'core':1,'archive':2}
        result.sort(key=lambda hit:(not hit.memory.get('later_user_record',False),-hit.score,order.get(hit.memory['kind'],1),-max((datetime.fromisoformat(s['at'].replace('Z','+00:00')).timestamp() for s in hit.memory['sources']),default=0)))
        unique={}
        timelines=[hit.memory for hit in result if hit.memory.get('episode_ids')]
        for hit in result:
            m=hit.memory
            if m['kind']=='archive' and any(set(m['source_message_ids'])<=set(t['source_message_ids']) and m['content'] in t['content'] for t in timelines):continue
            unique.setdefault(m['id'],hit)
        return list(unique.values())[:min(limit,6)]



def matched_excerpt(content, terms, limit):
    if len(content)<=limit:return content
    sentences=list(re.finditer(r'[^。！？!?\n]+[。！？!?\n]?',content))
    best=max(sentences,key=lambda m:sum(len(t) for t in terms if t in m[0].casefold()),default=None)
    if best and any(t in best[0].casefold() for t in terms) and len(best[0])<=limit:
        start,end=best.span()
        while True:
            previous=next((m for m in reversed(sentences) if m.end()==start),None)
            following=next((m for m in sentences if m.start()==end),None)
            if following and following.end()-start<=limit:end=following.end()
            elif previous and end-previous.start()<=limit:start=previous.start()
            else:break
        return content[start:end]
    match=next((content.casefold().find(t) for t in terms if t in content.casefold()),0)
    start=max(0,min(match-40,len(content)-limit))
    return content[start:start+limit]
