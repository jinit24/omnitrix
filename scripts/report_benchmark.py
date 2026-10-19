#!/usr/bin/env python3
"""Create portable benchmark evidence and charts from trusted local run results.

Requires matplotlib in the documentation environment, not the application runtime.
"""
import argparse
from collections import defaultdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import sqlite3
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

NAMES={'single':'Single worker','manager':'Research-first manager','adaptive':'Adaptive route'}
COLORS={'single':'#667085','manager':'#b54708','adaptive':'#087e8b'}
ARMS=['single','manager','adaptive']


def build(input_path,output):
    results=json.loads(input_path.read_text())
    if not results: raise ValueError('No completed benchmark arms')
    output.mkdir(parents=True,exist_ok=True)
    groups=defaultdict(list)
    for result in results:
        if result['mode']!='live': raise ValueError('Live evidence report requires live results')
        groups[result['task']].append(result)
    for task,runs in groups.items():
        if len({r['initial_tree'] for r in runs})!=1:
            raise ValueError('Different fixture trees in comparison: '+task)
    published=[]
    for result in results:
        row=json.loads(json.dumps(result))
        database=Path(row.pop('database'))
        trial=database.parent.parent.parent.name
        row['trial']=trial
        if not all(re.fullmatch(r'[A-Za-z0-9_-]+',s) for s in (trial,row['task'],row['arm'])):
            raise ValueError('Invalid artifact directory name')
        directory=output/'adaptive-evidence'/trial/row['task']/row['arm']
        directory.mkdir(parents=True,exist_ok=True)
        for index,check in enumerate(row['checks']):
            source=Path(check['artifact']);target=directory/f'check-{index}.log'
            target.write_bytes(source.read_bytes())
            check['artifact']=str(target.relative_to(output))
        repo=database.parent/'repo'
        patch=subprocess.check_output(['git','-C',str(repo),'diff',row['initial_tree'],row['candidate'],'--','service.py'],text=True)
        (directory/'candidate.patch').write_text(patch)
        row['candidate_patch']=str((directory/'candidate.patch').relative_to(output))
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
            row['implementation_diagnostics']=[dict(zip(('status','attempts','feedback'),r)) for r in db.execute('SELECT status,attempts,feedback FROM implementations')]
            sections=[]
            final=db.execute("SELECT body FROM decisions WHERE json_extract(body,'$.action')='complete' ORDER BY id DESC LIMIT 1").fetchone()
            if final:
                sections.append('# Manager acceptance review\n\n'+json.loads(final[0])['instructions'])
            handoff=db.execute("SELECT body FROM messages WHERE kind='implementation_handoff' ORDER BY created DESC LIMIT 1").fetchone()
            if handoff:
                report=json.loads(handoff[0]).get('report',{}).get('quality_report','')
                if report: sections.append('# Implementation worker evidence\n\n'+report)
            if sections:
                target=directory/'quality-review.md';target.write_text('\n\n'.join(sections)+'\n')
                row['quality_review']=str(target.relative_to(output))
        published.append(row)
    for task in groups:
        for arm in ARMS:
            if len([r for r in published if r['task']==task and r['arm']==arm])!=2:
                raise ValueError('This recorded comparison requires two runs per task and arm')
    metadata={'generated_at':datetime.now(timezone.utc).isoformat(),
        'source_results_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest(),
        'source_revisions':sorted({r['source_revision'] for r in published}),
        'measurement':f'Two repeated trials of {len(groups)} fixed fixture(s); descriptive results, not production or statistical superiority evidence.',
        'runs':published}
    if len(metadata['source_revisions'])!=1:
        raise ValueError('Comparison mixes source revisions')
    (output/'adaptive-comparison.json').write_text(json.dumps(metadata,indent=2)+'\n')
    plt.rcParams.update({'font.size':10,'svg.fonttype':'none','axes.spines.top':False,'axes.spines.right':False})
    tasks=list(groups)
    for metric,title,ylabel,filename in [
        ('wall_seconds','End-to-end time by fixture','Seconds (lower is faster)','adaptive-latency.svg'),
        ('input','Reported input tokens by fixture','Input tokens, including cached tokens','adaptive-tokens.svg')]:
        fig,ax=plt.subplots(figsize=(max(6,3*len(tasks)),4.8),layout='constrained')
        for i,arm in enumerate(ARMS):
            for t,task in enumerate(tasks):
                values=[r['wall_seconds'] if metric=='wall_seconds' else r['tokens']['input']
                        for r in groups[task] if r['arm']==arm and (metric=='wall_seconds' or r['tokens'] is not None)]
                if not values: continue
                x=t+(i-1)*.24
                median=statistics.median(values)
                ax.bar(x,median,width=.21,color=COLORS[arm],alpha=.85,label=NAMES[arm] if t==0 else None)
                ax.scatter([x]*len(values),values,s=20,color='#101828',zorder=3)
                ax.annotate(f'{median:.1f}' if metric=='wall_seconds' else f'{median/1000:.1f}k',
                            (x,max(values)),xytext=(0,6),textcoords='offset points',ha='center',fontsize=9)
        ax.set_title(title+'\nBars = median; dots = individual runs',loc='left',pad=18)
        ax.set_xticks(range(len(tasks)),tasks);ax.set_ylabel(ylabel);ax.set_ylim(bottom=0)
        ax.margins(y=.22);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
        ax.legend(loc='upper center',bbox_to_anchor=(.5,-.13),ncols=3,frameon=False,fontsize=8.5,handlelength=1.4,columnspacing=.8)
        fig.savefig(output/filename);fig.savefig(output/filename.replace('.svg','.png'),dpi=160);plt.close(fig)
        svg=output/filename
        svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n')
    rows=[]
    for arm in ARMS:
        runs=[r for r in published if r['arm']==arm]
        if not runs: continue
        complete=all(r['token_measurement_complete'] and r['tokens'] is not None for r in runs)
        rows.append(f"| {NAMES[arm]} | {sum(r['verified_outcome'] for r in runs)}/{len(runs)} | {sum(r['requirements_satisfied'] for r in runs)}/{sum(r['requirements_total'] for r in runs)} | {statistics.median(r['wall_seconds'] for r in runs):.1f} | {sum(r['wall_seconds'] for r in runs):.1f} | {sum(r['tokens']['input'] for r in runs):,} | {sum(r['tokens']['output'] for r in runs):,} | {'Complete' if complete else 'Partial: see raw records'} |")
    per_run=[]
    for r in sorted(published,key=lambda r:(r['trial'],r['task'],ARMS.index(r['arm']))):
        links=' · '.join(f"[check {i+1}]({c['artifact']})" for i,c in enumerate(r['checks']))
        if r.get('quality_review'): links += f" · [quality review]({r['quality_review']})"
        per_run.append(f"| {r['trial']} | {r['task']} | {NAMES[r['arm']]} | {r['wall_seconds']:.1f} | {r['requirements_satisfied']}/{r['requirements_total']} | [candidate]({r['candidate_patch']}) · {links} |")
    body='''# Recorded adaptive execution comparison

The comparison uses the same fixture trees, executable contracts and coding model
for all arms. The adaptive route removes prerequisite research for an explicitly
authorized bounded contract while retaining independent verification and final review.
The research-first arm includes the revised prompts and evaluation contract; it is
not an untouched historical implementation. Single-worker state may remain `active`
because that arm has no manager lifecycle; its candidate is independently scored.

![Latency comparison](adaptive-latency.svg)

| Route | Runs passing checks | Criteria passed | Median seconds | Total seconds | Input tokens | Output tokens | Token coverage |
| --- | --- | --- | --- | --- | --- | --- | --- |
'''+ '\n'.join(rows)+'''

![Token comparison](adaptive-tokens.svg)

Tokens are reported usage, including cached input, not billed dollar costs. The
manager uses gpt-5.4 and all coding arms use gpt-5.3-codex with medium reasoning.
Run order rotates across trials; two rotations do not fully balance order effects.
Role timings are retained in the raw records;
parallel role spans can overlap and must not generally be added as wall time.

These are two repetitions of three known, small Python fixtures, with acceptance
checks visible to workers. No held-out or production workload was measured. Shared
host/network/model latency varies, and six runs per arm do not establish statistical
significance or general architecture superiority. The adaptive route can remain
slower than a single worker: its independent verification and final manager review
have a real cost. Its measured benefit here is relative to unnecessary research-first
coordination. Compare larger and open-ended tasks before expanding rollout.

An earlier, broader exploratory run exposed inconsistent write-scope enforcement:
a research-first candidate was blocked for an unintended file, while a single-worker
candidate with the same mistake was scored without that scope check. Those original
18 results and the retrospective scope audit are preserved in
[exploratory evidence](adaptive-exploratory/README.md). They are not the final fair
comparison. The runs shown here use the corrected harness with the same write-scope
check on all arms and a bounded repair path for confirmed completed worker turns.

The frozen comparison checkpoint is `'''+metadata['source_revisions'][0]+'''`.
These timings precede the subsequent integration of upstream research recovery and
completion-review changes. They remain measurements of this frozen checkpoint; the
integrated runtime has separate regression, package and live smoke results below.
The runtime, native recovery, live parallel research and package delivery are validated separately in
[validation evidence](adaptive-validation.json). See the readable [native recovery review](adaptive-recovery.html) and [parallel research evidence](adaptive-wave.html). The initial failed routing run is
preserved in [calibration evidence](adaptive-calibration.json); it is excluded from
this fixed comparison after the unavailable-action bug was corrected.

[Machine-readable values and stage timings](adaptive-comparison.json) ·
[Mechanism, practice references, monitoring and rollback](../ADAPTIVE_EXECUTION.md)

## Candidate and check evidence

Each candidate patch is relative to the identical fixture tree. Check logs come from
independent scoring of an immutable candidate checkout; they are not worker claims.

| Trial | Fixture | Route | Seconds | Criteria | Artifacts |
| --- | --- | --- | --- | --- | --- |
'''+ '\n'.join(per_run)+'\n'
    body=body.replace('two repetitions of three known, small Python fixtures', ('two repetitions of one known, small Python fixture' if len(groups)==1 else f'two repetitions of {len(groups)} known, small Python fixtures')).replace('six runs per arm', f"{2*len(groups)} runs per arm")
    (output/'adaptive-comparison.md').write_text(body)
    print(json.dumps({'runs':len(published),'artifacts':str(output),'source_revisions':metadata['source_revisions']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    build(args.input.resolve(),args.output.resolve())
