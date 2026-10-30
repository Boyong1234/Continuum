"""Local read-only data viewer. No application startup, Provider or Sync calls."""
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def active_database():
    command="Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'start_continuum_gateway.py' } | Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress"
    value=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,encoding='utf8',errors='replace',check=True)
    lines=json.loads(value.stdout) if value.stdout.strip() else []
    if isinstance(lines,str):lines=[lines]
    datasets=set()
    for line in lines:
        match=re.search(r'--dataset"?\s+(?:"([^"]+)"|(\S+))',line)
        if match:
            folder=Path(match[1] or match[2]).resolve()
            if folder.is_relative_to(ROOT/'data'):datasets.add(str(folder/'continuum.db'))
    if len(datasets)!=1:raise ValueError('无法唯一确定正在运行的QQ数据库；请显式指定 --db，不会回退到旧v3。')
    return Path(datasets.pop())


@contextmanager
def readonly(path):
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        yield c
    finally:
        c.rollback()
        c.close()


def overview(c,path):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def count(table):return c.execute('SELECT count(*) FROM '+table).fetchone()[0] if table in tables else None
    states=lambda table:dict(c.execute('SELECT '+('state' if table!='memory_extraction_jobs' else 'status')+',count(*) FROM '+table+' GROUP BY 1')) if table in tables else None
    runtime=dict(c.execute('SELECT enabled,paused,initialized,updated_at FROM memory_runtime').fetchone()) if 'memory_runtime' in tables else None
    now=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    active=c.execute("SELECT count(*) FROM memory_lifecycle l WHERE l.state='active' AND l.evidence_level IN ('user_explicit','user_confirmed') AND (l.valid_from IS NULL OR l.valid_from<=?) AND (l.valid_to IS NULL OR l.valid_to>?) AND NOT EXISTS(SELECT 1 FROM memory_suppressions x WHERE x.kind='semantic' AND x.target_id=l.memory_id) AND NOT EXISTS(SELECT 1 FROM memory_sources s JOIN memory_exclusions e ON e.message_id=s.message_id WHERE s.memory_id=l.memory_id)",(now,now)).fetchone()[0] if 'memory_suppressions' in tables else None
    return dict(database=str(path),schema_version=c.execute('SELECT version FROM schema_info').fetchone()[0],database_bytes=path.stat().st_size,wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,journal_mode=c.execute('PRAGMA journal_mode').fetchone()[0],conversations=count('conversations'),messages=count('messages'),message_roles=dict(c.execute('SELECT role,count(*) FROM messages GROUP BY role')),qq_messages=c.execute("SELECT count(*) FROM messages WHERE json_extract(metadata,'$.channel')='qq_onebot_v11'").fetchone()[0],episodes=count('episodes'),episode_states=states('episodes'),core_total=count('memories'),core_states=states('memory_lifecycle'),active_core=active,memory_sources=count('memory_sources'),episode_sources=count('episode_sources'),memory_revisions=count('memory_revisions'),memory_revision_audit=count('memory_revision_audit'),extraction_jobs=count('memory_extraction_jobs'),extraction_job_states=states('memory_extraction_jobs'),last_completed_extraction_job_at=c.execute("SELECT max(updated_at) FROM memory_extraction_jobs WHERE status='succeeded'").fetchone()[0] if 'memory_extraction_jobs' in tables else None,last_memory_sync='不适用：当前schema没有独立Sync运行时间字段；任务完成时间不等于Sync时间',runtime=runtime,latest_message_at=c.execute('SELECT max(created_at) FROM messages').fetchone()[0])


def sources(c,ids,full):
    result=[]
    for id in ids:
        row=c.execute('SELECT id,role,created_at,content FROM messages WHERE id=?',(id,)).fetchone()
        if row:result.append(dict(row) if full else dict(id=row['id'],role=row['role'],created_at=row['created_at']))
    return result


def preview(value,full):
    return value if full else re.sub(r'\s+',' ',value)[:64]+('…' if len(value)>64 else '')


def memory_status(c,path):
    sys.path.insert(0,str(ROOT/'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.services.memory.runtime.control import MemoryControl
    from app.services.memory.runtime.sync import MemorySync
    engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(path.as_uri()+'?mode=ro',uri=True))
    try:
        control=MemoryControl(sessionmaker(engine),None)
        status=control.status()
        if status['supported']:status.update(MemorySync(control).pending())
    finally:engine.dispose()
    command="Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'app.services.memory.runtime' -and $_.CommandLine -match '--watch' } | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    processes=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,encoding='utf8',errors='replace')
    status['watch_inspection']='ok' if processes.returncode==0 else 'unavailable'
    rows=json.loads(processes.stdout) if processes.returncode==0 and processes.stdout.strip() else []
    if isinstance(rows,dict):rows=[rows]
    watches=[]
    for row in rows:
        line=row['CommandLine']
        match=re.search(r'--database"?\s+(?:"([^"]+)"|(\S+))',line)
        if not match or Path(match[1] or match[2]).resolve()!=path:continue
        options={}
        for name in ('interval-hours','min-new-messages','max-batches'):
            value=re.search(r'--'+name+r'"?\s+"?([\d.]+)',line)
            options[name]=float(value[1]) if value else {'interval-hours':24,'min-new-messages':30,'max-batches':1}[name]
        watches.append(dict(pid=row['ProcessId'],parent_pid=row['ParentProcessId'],**options))
    # Windows venv launcher and its Python child are one logical watch.
    parents={row['parent_pid'] for row in watches}
    status['watch_processes']=[row for row in watches if row['pid'] not in parents]
    status['watch_running']=bool(watches) if status['watch_inspection']=='ok' else None
    status['batch_state']='paused' if status.get('paused') else 'extracting' if status.get('enabled') else 'idle'
    latest=c.execute('SELECT id,status,attempts,last_error,updated_at FROM memory_extraction_jobs ORDER BY updated_at DESC,id DESC LIMIT 1').fetchone()
    status['latest_job']=dict(latest) if latest else None
    if latest:
        status['latest_job']['decisions']=[dict(row) for row in c.execute('SELECT kind,action,disposition,reason,count(*) AS count FROM extraction_decisions WHERE job_id=? GROUP BY kind,action,disposition,reason',(latest['id'],))]
    status['time_note']='last_processed_at 是最后成功任务时间，不是每次 watch 扫描时间；enabled=false 可表示批次间待机。'
    return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,help='显式选择一个库；默认检测运行中QQ后端，绝不混合多个库')
    parser.add_argument('action',choices=['overview','recent','episodes','core','search','timeline','storage','memory-status','experience-candidates'])
    parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--id');parser.add_argument('--conversation');parser.add_argument('--query')
    parser.add_argument('--full',action='store_true',help='显式本地完整展示私人正文；不会写文件')
    args=parser.parse_args()
    if not 1<=args.limit<=100:parser.error('--limit must be 1..100')
    if args.action=='search' and not args.query:parser.error('search needs --query')
    if args.action=='timeline' and not args.id:parser.error('timeline needs --id EPISODE_ID')
    db=args.db.resolve() if args.db else active_database()
    with readonly(db) as c:
        if args.action=='overview':result=overview(c,db)
        elif args.action=='memory-status':result=memory_status(c,db)
        elif args.action=='storage':
            result=dict(active_database=str(db),persona=str(db.parent/'persona'),pilot_config=str(db.parent/'memory-pilot.env'),provider_config=str(ROOT/'.env'),deeptalk_originals=str(ROOT/'data/imports/deeptalk'),release_backups=str(ROOT/'data/rehearsals/k1-7-release-20261029'),other_backups=str(ROOT/'data/backups'),note='只显示位置，不读取凭据内容；其他v3/合成库未纳入统计')
        elif args.action=='recent':
            rows=c.execute('SELECT id,conversation_id,role,created_at,content FROM messages '+('WHERE conversation_id=? ' if args.conversation else '')+'ORDER BY created_at DESC,rowid DESC LIMIT ?',([args.conversation] if args.conversation else [])+[args.limit]).fetchall()
            result=[dict(r)|dict(content=preview(r['content'],args.full)) for r in reversed(rows)]
        elif args.action=='episodes':
            rows=c.execute('SELECT * FROM episodes '+('WHERE id=? ' if args.id else '')+'ORDER BY ended_at DESC LIMIT ?',([args.id] if args.id else [])+[args.limit]).fetchall();result=[]
            for r in rows:
                ids=[x[0] for x in c.execute('SELECT message_id FROM episode_sources WHERE episode_id=?',(r['id'],))]
                candidate=json.loads(r['candidate_json'])
                points=[dict(perspective=p['perspective'],content=preview(p['content'],args.full),source_message_ids=list(dict.fromkeys(e['message_id'] for e in p['evidence']))) for p in candidate.get('discussion',[])]
                result.append(dict(id=r['id'],title=r['title'],state=r['state'],started_at=r['started_at'],ended_at=r['ended_at'],content=preview(r['content'],args.full),discussion=points,sources=sources(c,ids,args.full),supersedes=r['supersedes'],linked_core=[x[0] for x in c.execute('SELECT memory_id FROM episode_memory_links WHERE episode_id=?',(r['id'],))]))
        elif args.action=='core':
            rows=c.execute('SELECT m.*,l.state,l.evidence_level,l.valid_from,l.valid_to,l.superseded_by,l.version FROM memories m LEFT JOIN memory_lifecycle l ON l.memory_id=m.id '+('WHERE m.id=? ' if args.id else '')+'ORDER BY m.updated_at DESC LIMIT ?',([args.id] if args.id else [])+[args.limit]).fetchall();result=[]
            for r in rows:
                ids=[x[0] for x in c.execute('SELECT message_id FROM memory_sources WHERE memory_id=?',(r['id'],))]
                result.append(dict(r)|dict(content=preview(r['content'],args.full),sources=sources(c,ids,args.full),revision_ids=[x[0] for x in c.execute('SELECT id FROM memory_revisions WHERE memory_id=?',(r['id'],))]))
        else:result=None
    if args.action=='experience-candidates':
        sys.path.insert(0,str(ROOT/'backend'))
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.services.context.experience import feedback_candidates
        engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(db.as_uri()+'?mode=ro',uri=True))
        try:
            with Session(engine) as session:result=feedback_candidates(session,args.limit)
            for candidate in result:
                for turn in candidate['turns']:turn['content']=preview(turn['content'],args.full)
        finally:engine.dispose()
    if args.action in ('search','timeline'):
        sys.path.insert(0,str(ROOT/'backend'))
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.services.memory.retrieval import LocalHistoryRetriever,RetrievalRequest
        engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(db.as_uri()+'?mode=ro',uri=True))
        try:
            if args.action=='timeline':
                from app.services.memory.understanding.continuity import EpisodeStore
                with sessionmaker(engine)() as session:
                    events=EpisodeStore(session).timeline(args.id,limit=min(args.limit,3))
                with readonly(db) as c:
                    result=[dict(id=e['id'],title=e['title'],content=preview(e['content'],args.full),
                                 at=e['ended_at'],previous_episode_ids=e.get('previous_episode_ids',[]),
                                 progress_evidence_ids=e.get('progress_evidence_ids',[]),sources=sources(c,e['source_message_ids'],args.full)) for e in events]
            else:
                hits=LocalHistoryRetriever(sessionmaker(engine)).retrieve(RetrievalRequest(args.query,limit=args.limit))
                with readonly(db) as c:
                    result=[dict(id=h.memory['id'],kind=h.memory.get('kind','core'),content=preview(h.memory['content'],args.full),perspective=h.memory.get('perspective'),sources=sources(c,h.memory.get('source_message_ids',[]),args.full)) for h in hits]
        finally:engine.dispose()
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf8')
    try:main()
    except (ValueError,sqlite3.Error) as exc:raise SystemExit(str(exc))
