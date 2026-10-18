#!/usr/bin/env python3
"""Deterministic full loop with a failed candidate, independent tests, and repair."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT))
from benchmark import MockCoding,run_arm
from omnitrix.store import Store


async def main(output):
    task=json.loads((ROOT/'benchmarks/tasks.json').read_text())[0]
    class RepairCoding(MockCoding):
        async def start(self,workspace,instructions,**kwargs):
            self.solution=task['initial'] if self.calls==0 else task['solution']
            return await super().start(workspace,instructions,**kwargs)
    # Reuse the real manager/research/authorization/verification harness; only model reasoning and coding are doubles.
    result=await run_arm(task,'manager',output,False,True,12,runtime_override=RepairCoding(task['solution']))
    store=Store(output/'state.db')
    print(json.dumps({'mode':'deterministic mocked agents with real SQLite/Git/test processes',
        'manager_actions':[json.loads(r[0])['action'] for r in store.db.execute('SELECT body FROM decisions')],
        'research':[dict(r) for r in store.db.execute('SELECT stage,status,attempts FROM research_jobs')],
        'evaluations':[json.loads(r[0]) for r in store.db.execute('SELECT evaluation FROM research_runs WHERE evaluation IS NOT NULL')],
        'verifications':[dict(r) for r in store.db.execute('SELECT candidate,requirement,passed FROM verifications ORDER BY created')],
        'result':result},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'.omnitrix/demo')
    args=p.parse_args()
    if args.output.exists(): p.error('Use a new output directory; existing artifacts are preserved')
    asyncio.run(main(args.output.resolve()))
