"""Read-only Archive scan; a private reference file owns its incremental cursor."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.services.context.experience import FEEDBACK_PATTERN,correction_scope,assertable_feedback


def scan_feedback(database,output):
    database,output=Path(database).resolve(),Path(output).resolve()
    state=json.loads(output.read_text('utf8')) if output.exists() else dict(schema_version=1,cursors={},candidates=[])
    examined=0;added=0
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
        for binding in connection.execute('SELECT id,conversation_id FROM channel_bindings').fetchall():
            cursor=state['cursors'].get(binding['id'],0)
            turns=connection.execute("SELECT * FROM channel_turns WHERE binding_id=? AND sequence>? ORDER BY sequence",(binding['id'],cursor)).fetchall()
            for turn in turns:
                # An open earlier turn will be revisited after its reply is durable.
                if turn['state'] in ('open','queued','generating'):break
                state['cursors'][binding['id']]=turn['sequence']
                if turn['state']!='completed':continue
                examined+=1
                previous=connection.execute('SELECT * FROM channel_turns WHERE binding_id=? AND sequence<? ORDER BY sequence DESC LIMIT 1',(binding['id'],turn['sequence'])).fetchone()
                target=previous['reply_message_id'] if previous and previous['state']=='completed' else None
                sources=connection.execute('SELECT m.id,m.role,m.content,m.created_at,m.metadata FROM messages m JOIN channel_messages c ON c.canonical_message_id=m.id WHERE c.turn_id=? ORDER BY c.rowid',(turn['id'],)).fetchall()
                for source in sources:
                    if not assertable_feedback(SimpleNamespace(role=source['role'],content=source['content'],metadata_json=json.loads(source['metadata']))):continue
                    scope=correction_scope(source['content'])
                    match=FEEDBACK_PATTERN.search(source['content'])
                    if scope and not match:match=re.search(r'.+',source['content'])
                    if not match:continue
                    id='qq-style-'+hashlib.sha256((source['id']+':'+str(target)).encode()).hexdigest()[:24]
                    state['candidates'].append(dict(experience_id=id,source='qq',conversation_id=binding['conversation_id'],
                        channel_turn_id=turn['id'],sequence=turn['sequence'],user_message_id=source['id'],
                        assistant_message_id=target,followup_reply_id=turn['reply_message_id'],
                        message_ids=[x for x in [target,source['id'],turn['reply_message_id']] if x],
                        source_time=source['created_at'],feedback_evidence=dict(role='user',start=match.start(),end=match.end(),
                            quote_sha256=hashlib.sha256(match[0].encode()).hexdigest(),
                            strength='explicit_expression_not_quality_approval',
                            scope='current_requirement' if '想' in match[0] or '不用每次' in match[0] else 'response_feedback',
                            target_relation='preceding_reply_candidate_requires_review' if target else 'unresolved'),
                        candidate_status='unreviewed',approved=False))
                    candidate=state['candidates'][-1]
                    target_text=connection.execute('SELECT content FROM messages WHERE id=?',(target,)).fetchone() if target else None
                    if scope and scope['kind']=='attribution_correction':
                        aspect=re.search(r'比较|证明',target_text['content']) if target_text else None
                        if not aspect or not any(t in target_text['content'] for t in scope['terms']):scope=None
                        else:
                            candidate['assistant_evidence']=dict(start=aspect.start(),end=aspect.end(),
                                sha256=hashlib.sha256(aspect[0].encode()).hexdigest(),relation='adjacent_reply_matching_aspect')
                    if scope and target and len(source['content'])<=220:
                        candidate.update(candidate_status='usable',applicability=scope,
                            admission='explicit_scoped_user_requirement',
                            correction_evidence=dict(start=0,end=len(source['content']),
                                sha256=hashlib.sha256(source['content'].encode()).hexdigest()),
                            revisions=[dict(action='admitted',reason='explicit_scoped_user_requirement',
                                            source_message_id=source['id'])])
                        if any(r['experience_id']!=id and r.get('candidate_status')=='usable'
                               and r.get('applicability')==scope for r in state['candidates']):
                            candidate['candidate_status']='alignment_required'
                    added+=1
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix(output.suffix+'.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8')
    temporary.replace(output)
    return dict(examined_completed_turns=examined,added_candidates=added,total_candidates=len(state['candidates']),
                cloud_calls=0,production_writes=0)


def replace_experience(output,old_id,new_id,reason):
    """Explicit local review, not an automatic latest-wins policy."""
    output=Path(output)
    state=json.loads(output.read_text('utf8'));rows={r['experience_id']:r for r in state['candidates']}
    old=rows[old_id]
    if old['candidate_status']!='usable':raise ValueError('Only usable experiences can be retired')
    if new_id is None:
        old['candidate_status']='archived'
        old.setdefault('revisions',[]).append(dict(action='archived',reason=reason))
    else:
        new=rows[new_id]
        if old_id==new_id or new['candidate_status'] not in ('usable','alignment_required'):
            raise ValueError('Replacement requires a distinct scoped experience')
        if old['applicability']!=new['applicability']:raise ValueError('Different scopes coexist; do not supersede')
        old.update(candidate_status='superseded',superseded_by=new_id)
        new['candidate_status']='usable'
        change=dict(action='superseded',reason=reason,source_message_id=new['user_message_id'],replacement=new_id)
        old.setdefault('revisions',[]).append(change)
        new.setdefault('revisions',[]).append(dict(action='replaces',previous=old_id,reason=reason))
    temporary=output.with_suffix(output.suffix+'.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8');temporary.replace(output)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='Private, per-dataset reference index; never share across datasets')
    parser.add_argument('--watch',action='store_true',help='Optional local-only low-frequency scan; no model or Memory Sync')
    parser.add_argument('--interval-hours',type=float,default=24)
    parser.add_argument('--stop-file',type=Path)
    parser.add_argument('--replace-id',help='Reviewed old usable experience to supersede')
    parser.add_argument('--with-id',help='New usable experience with the same applicability')
    parser.add_argument('--reason',help='Explicit user correction and review rationale')
    args=parser.parse_args()
    if args.replace_id:
        if not args.reason:parser.error('Review requires --reason; omit --with-id to retire without replacement')
        replace_experience(args.output,args.replace_id,args.with_id,args.reason);return
    if not 1<=args.interval_hours<=168:parser.error('--interval-hours must be 1..168')
    try:
        while not (args.stop_file and args.stop_file.exists()):
            print(json.dumps(scan_feedback(args.database,args.output)),flush=True)
            if not args.watch:return
            deadline=time.monotonic()+args.interval_hours*3600
            while time.monotonic()<deadline:
                if args.stop_file and args.stop_file.exists():return
                time.sleep(30)
    except KeyboardInterrupt:return


if __name__=='__main__':main()
