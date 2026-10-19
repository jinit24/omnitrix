#!/usr/bin/env python3
"""Opt-in live validation of native recovery, direct execution and a research wave.

Recovery uses a copy of a failed isolated benchmark DB, preserving the source ledger.
Its already-authorized worktree and native thread are retained, not recreated.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from omnitrix.implementation import Implementations,git
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan,ResearchBudget
from omnitrix.runtime import CodexRuntime
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from scripts.benchmark import run_arm


async def recover(source,output):
    directory=output/'recovery';directory.mkdir()
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as original, sqlite3.connect(directory/'state.db') as copy:
        original.backup(copy)
    store=Store(directory/'state.db')
    goal=store.db.execute('SELECT id FROM goals').fetchone()[0]
    implementation=dict(store.db.execute('SELECT * FROM implementations').fetchone())
    auth,spec=Implementations(store).auth(implementation['authorization'])
    if spec.allowed_paths!=['service.py'] or implementation['status']!='uncertain' or 'outside explicit authorization' not in implementation['feedback']:
        raise ValueError('Recovery source must be the known isolated fixture scope failure')
    task=store.db.execute("SELECT task_id FROM task_runtime WHERE json_extract(spec,'$.implementation_id')=? ORDER BY started DESC LIMIT 1",(implementation['id'],)).fetchone()[0]
    event_cursor=store.db.execute('SELECT max(id) FROM activity').fetchone()[0]
    owner=await Supervisor(store,goal,runtime=CodexRuntime()).open()
    started=time.monotonic()
    try:
        result=await owner.reconcile(task)
        if result.get('implementation',{}).get('status')!='repair':
            raise RuntimeError('Exact completed native turn was not reconciled into a bounded repair')
        owner.autonomy=6
        deadline=started+300
        while time.monotonic()<deadline:
            if not owner.work:
                if owner.autonomy<1: break
                await owner.advance()
            if owner.status()['goal']['status']=='completed': break
            await asyncio.sleep(.1)
        row=Implementations(store).get(implementation['id'])
        events=[json.loads(r[0]) for r in store.db.execute("SELECT detail FROM activity WHERE id>? AND kind='implementation_turn'",(event_cursor,))]
        checks=[dict(r) for r in store.db.execute('SELECT requirement,passed,candidate,artifact FROM verifications WHERE implementation_id=?',(row['id'],))]
        report={'source_database':str(source),'goal_status':owner.status()['goal']['status'],
            'implementation_status':row['status'],'attempts':row['attempts'],'candidate':row['candidate'],
            'repair_turns':events,'checks':checks,'seconds':time.monotonic()-started,
            'passed':row['status']=='completed' and owner.status()['goal']['status']=='completed' and bool(events) and all(e['resumed'] for e in events)}
        (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    finally:
        await owner.close();store.db.close()


async def wave(output):
    directory=output/'research-wave';directory.mkdir()
    repo=directory/'sources';repo.mkdir()
    (repo/'polling.txt').write_text('Authoritative fixture configuration: poll_seconds = 30. This is a configuration fact, not a measured latency claim.\n')
    (repo/'retry.txt').write_text('Authoritative fixture configuration: max_attempts = 3. This is a configuration fact, not a measured reliability claim.\n')
    store=Store(directory/'state.db')
    goal=store.create_goal('Independently verify the two scoped configuration facts',['Cite the actual configured values; do not infer production impact'],repo)
    research=Research(store);research.configure(goal,max_runs=4)
    owner=await Supervisor(store,goal,runtime=CodexRuntime(),research=research).open()
    plans=[ResearchPlan(scope=[filename],question=f'What evidence supports the literal fact {fact}?',
        hypothesis=f'The scoped authoritative fixture states {fact}',falsification='The scoped file states a different value or does not contain this key',
        budget=ResearchBudget(tool_calls=4,seconds=90,attempts=1))
        for filename,fact in [('polling.txt','poll_seconds = 30'),('retry.txt','max_attempts = 3')]]
    started=time.monotonic()
    try:
        result=await owner.dispatch(Decision(action='research_batch',rationale='Independent read-only configuration questions',
            instructions='Verify only the stated configuration facts',task_id=None,priority=1,research_plans=plans,
            independence_reason='Separate immutable configuration facts; neither answer is input to the other'))
        await asyncio.gather(*list(owner.work.values()))
        spans=[]
        for task in result['task_ids']:
            first=store.db.execute("SELECT created FROM activity WHERE task_id=? AND kind='worker_started'",(task,)).fetchone()[0]
            last=store.db.execute("SELECT created FROM activity WHERE task_id=? AND kind='worker_finished'",(task,)).fetchone()[0]
            spans.append({'task_id':task,'start':first,'finish':last})
        jobs=[dict(r) for r in store.db.execute('SELECT id,status FROM research_jobs')]
        spent=store.db.execute('SELECT spent FROM research_limits').fetchone()[0]
        overlap=max(s['start'] for s in spans)<min(s['finish'] for s in spans)
        report={'jobs':jobs,'role_runs_spent':spent,'spans':spans,'overlap':overlap,'seconds':time.monotonic()-started,
            'passed':all(j['status']=='supported' for j in jobs) and spent==4 and overlap}
        (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    finally:
        await owner.close();store.db.close()


async def main(args):
    if not args.allow_code_modifications or not os.getenv('OPENAI_API_KEY'):
        raise SystemExit('Requires API authentication and --allow-code-modifications for isolated fixtures')
    args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
    report={'source_revision':git(ROOT,'rev-parse','HEAD'),'source_dirty':bool(git(ROOT,'status','--porcelain'))}
    if args.recovery_source:
        report['recovery']=await recover(args.recovery_source.resolve(),args.output)
        (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    task=json.loads((ROOT/'benchmarks/tasks.json').read_text())[1]
    report['direct']=await run_arm(task,'adaptive',args.output/'direct',True,True,6,arm_seconds=300)
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    report['research_wave']=await wave(args.output)
    report['passed']=report.get('recovery',{'passed':True})['passed'] and report['direct']['verified_outcome'] and report['direct']['goal_status']=='completed' and report['research_wave']['passed']
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'report':str(args.output/'result.json')}))
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-code-modifications',action='store_true')
    parser.add_argument('--recovery-source',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(main(parser.parse_args()))
