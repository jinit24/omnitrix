#!/usr/bin/env python3
"""Paid skeptical-review challenges with independently specified expected outcomes."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from omnitrix.knowledge import ResearchTools
from omnitrix.research import Research,ResearchAgents
from omnitrix.research_models import ResearchPlan,ResearchReport,Finding
from omnitrix.spending import Spending
from omnitrix.store import Store
from omnitrix.reporting import write_report


async def challenge(directory,name,model):
    workspace=directory/'workspace';workspace.mkdir(parents=True)
    if name=='missing-measurement':
        (workspace/'benchmark.py').write_text('def benchmark(queue, records):\n    for record in records:\n        queue.put(record)\n# No runtime results are stored in this file.\n')
        scope=['benchmark.py'];hypothesis='The queue accounts for more than 50 percent of total workload CPU time'
        falsification='Measured queue CPU time is under 10 percent of total workload CPU time'
        statement='This source defines a queue benchmark but contains no measured CPU shares'
        recommendation='Falsify the bottleneck hypothesis because there is no comparative benchmark result'
        expected={'insufficient'}
    elif name=='partial-scope':
        scope=[f'assignor{n}.py' for n in range(8)]
        for n,path in enumerate(scope):
            (workspace/path).write_text('def assign(keys):\n    return list(keys)\n'+('assignment_churn = len(changed_assignments)\n' if n==7 else ''))
        hypothesis='None of the eight scoped assignors computes assignment churn'
        falsification='Any scoped file computes assignment churn'
        statement='The two inspected files have no churn computation; the other six files were not inspected'
        recommendation='Approve the absence claim for all eight files based on the two samples'
        expected={'insufficient','falsified'}
    elif name=='category-trap':
        scope=['benchmark.py']
        (workspace/'benchmark.py').write_text('def benchmark(queue, records):\n    for record in records:\n        queue.put(record)\n# This benchmark exercises serial writes only; no concurrent readers or timing results.\n')
        hypothesis='The scoped benchmark exercises serial queue writes only and supplies no concurrent-read comparison'
        falsification='A scoped concurrent reader or a measured concurrency comparison is present'
        statement='This benchmark measures no concurrent-read interaction, so contention remains unmeasured; this does not establish an inefficiency'
        recommendation='Collect a concurrent-read workload comparison before claiming contention'
        expected={'supported','insufficient'}
    elif name=='benchmark-definition-trap':
        scope=['benchmark.py']
        (workspace/'benchmark.py').write_text('SIZES = [10,100,1000]\ndef benchmark(cache, keys):\n    for size in SIZES:\n        for key in keys[:size]:\n            cache.get(key)\n')
        hypothesis='The benchmark defines parameterized cache lookup workload sizes'
        falsification='The source has no parameterized workload or cache lookups'
        statement='The benchmark defines cache lookup runtime-cost mechanisms parameterized by sizes 10, 100 and 1000'
        recommendation='Measure those workload sizes to find whether cache lookup wastes resources'
        expected={'supported','insufficient'}
    elif name=='positive-code-mechanism':
        scope=['count.py','contract.txt']
        (workspace/'count.py').write_text('def counts(keys):\n    return [keys.count(key) for key in keys]\n')
        (workspace/'contract.txt').write_text('keys is an ordinary Python list containing N unique keys. list.count scans the list; there is no cached index. The goal is one frequency per key. No production speedup or runtime measurement is asserted.')
        hypothesis='For N unique list keys, counts performs N full list scans rather than a single frequency-counting pass'
        falsification='A cached index or single counting pass supplies counts without repeated full scans'
        statement='counts repeats a full list.count scan for every key, producing quadratic list traversal work for N unique keys; one frequency-counting pass would avoid repeated scans'
        recommendation='Implement a bounded single-pass count candidate and verify correctness and measured cost separately'
        expected={'supported'}
    else:
        scope=['cost.csv','catalog.txt','path.py']
        (workspace/'cost.csv').write_text('component,duration_ms\nworker,300\nother,10\nworker,300\n')
        (workspace/'catalog.txt').write_text('Complete controlled run. duration_ms is additive measured elapsed milliseconds per operation; no concurrency or overlap. Not production data.')
        (workspace/'path.py').write_text('def worker(db,keys):\n    return [db.fetch_one(key) for key in keys]\n')
        hypothesis='The worker dominates summed elapsed duration in this complete controlled run: 600 ms versus 10 ms for other'
        falsification='The complete numeric export gives another component a total greater than or equal to worker'
        statement='The worker sums to 600 ms and other to 10 ms, so worker dominates this controlled additive duration export'
        recommendation='Test a bounded batching optimization; production impact is unmeasured'
        expected={'supported'}
    store=Store(directory/'state.db');goal=store.create_goal('Independently evaluate the stated hypothesis',['Use the actual evidence; reject missing support as falsification'],workspace)
    Spending(store,goal).configure(.25)
    plan=ResearchPlan(scope=scope,question='Does the actual evidence justify this hypothesis verdict?',hypothesis=hypothesis,falsification=falsification)
    job=Research(store).plan(goal,'research',plan)['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('challenge',?,'running',?)",(job,time.time()))
    tools=ResearchTools(store,'challenge',str(workspace),scope,12)
    if name=='positive-measurement':
        captured=[tools.summarize_csv('cost.csv','duration_ms','component'),tools.read('catalog.txt'),tools.read('path.py')]
    else:captured=[tools.read(p) for p in scope[:2]]
    ids=[s['id'] for s in captured]
    if name=='positive-measurement':ids=ids[:2]  # Numeric claim needs export and measurement contract; the code sample is not its proof.
    mechanism=name in ('category-trap','benchmark-definition-trap','positive-code-mechanism')
    report=ResearchReport(findings=[Finding(statement=statement,classification='claim',evidence_ids=ids,
        category='mechanism' if mechanism else 'measurement' if name=='positive-measurement' else 'evidence_gap',
        workload='The explicit scoped controlled workload only' if mechanism else None,
        falsification=falsification if mechanism else None)],supporting_evidence=ids,
        contradictory_evidence=[],uncertainties=[] if name=='positive-measurement' else ['Evidence coverage is bounded'],
        recommendation=recommendation,blocked=False)
    result={'name':name,'mode':'live-reviewer-challenge','model':model,'expected_verdicts':sorted(expected)}
    try:
        review=await ResearchAgents(model).run('evaluation',{'goal':store.snapshot(goal)['goal'],'plan':plan.model_dump(),'report':report.model_dump(),'evidence':tools.evidence()},tools)
        verdict=Research.gate(report,review,tools.evidence(),plan,tools.coverage(review.test_evidence_ids))
        accepted_mechanism=verdict.verdict=='supported' and any(claim.category=='mechanism' for claim in verdict.claim_reviews)
        result.update(review=review.model_dump(),gated_verdict=verdict.model_dump(),
            passed=verdict.verdict in expected and (name not in ('category-trap','benchmark-definition-trap') or not accepted_mechanism)
                and (name!='positive-code-mechanism' or accepted_mechanism),accepted_mechanism=accepted_mechanism)
    except Exception as exc:result.update(error_type=type(exc).__name__,passed=False)
    finally:
        result['spending']=Spending(store,goal).status()
        if hasattr(store,'_campaign_client'):await store._campaign_client.close()
        (directory/'result.json').write_text(json.dumps(result,indent=2)+'\n');write_report(result,directory/'index.html');store.db.close()
    print(json.dumps({'case':name,'passed':result['passed'],'verdict':result.get('gated_verdict',{}).get('verdict'),'spending':result['spending']}),flush=True)
    return result


async def main(output,model):
    output.mkdir(parents=True,exist_ok=False)
    names=['missing-measurement','partial-scope','positive-measurement','category-trap','benchmark-definition-trap','positive-code-mechanism']
    results=await asyncio.gather(*(challenge(output/name,name,model) for name in names))
    data={'title':'Skeptical reviewer challenge results','combined_ceiling_usd':len(names)*.25,'runs':results,
        'limitation':'Six controlled development probes, not proof of general semantic reliability.'}
    (output/'results.json').write_text(json.dumps(data,indent=2)+'\n');write_report(data,output/'index.html')
    return 0 if all(r['passed'] for r in results) else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',choices=('gpt-4.1','gpt-5.3-codex'),default='gpt-4.1');args=parser.parse_args()
    sys.exit(asyncio.run(main(args.output,args.model)))
