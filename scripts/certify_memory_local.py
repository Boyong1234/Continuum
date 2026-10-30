"""Optional existing offline release gate; only explicit target certification writes."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from gateway_common import ROOT,isolated
sys.path.insert(0,str(ROOT/'backend'))
from app.database import setup_database
from app.maintenance.backup import upgrade_copy
from app.services.memory.runtime.evaluation import evaluate_independent
from app.services.memory.runtime.control import MemoryControl

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',required=True);a=p.parse_args()
    target=isolated(a.database)
    if not target.is_file():raise ValueError('Initialize your empty archive first')
    folder=ROOT/'data/quality-checks';folder.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(dir=folder))
    engine,_=setup_database('sqlite:///'+(stage/'source.db').as_posix());engine.dispose()
    upgrade_copy(stage/'source.db',ROOT/'templates/persona',stage/'v7',stage/'backups',allow_plaintext=True,target_version=7)
    engine,sessions=setup_database('sqlite:///'+(stage/'v7/continuum.db').as_posix())
    report=evaluate_independent(ROOT/'release/memory-quality-cases.json',sessions);engine.dispose()
    (stage/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    engine,sessions=setup_database('sqlite:///'+target.as_posix())
    try:
        if not MemoryControl(sessions,None).certify(report):raise ValueError('Existing quality gate failed')
    finally:engine.dispose()
    print('Existing offline quality gate passed. No cloud calls; no extraction authorization granted.')
