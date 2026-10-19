#!/usr/bin/env python3
"""Research-only autonomous trials with competing clues and a hidden scoring oracle.

No fixture answers or prescribed plans are given to the live manager. --harness-only
checks data/tool plumbing with scripted agents; it is never a model-quality result.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.control import Control
from omnitrix.knowledge import Knowledge
from omnitrix.manager import AgentBrain
from omnitrix.models import Decision
from omnitrix.research import Research, ResearchAgents
from omnitrix.research_models import DiscoveryReport, Evaluation, Finding, MapEntry, Opportunity, ResearchPlan, ResearchReport
from omnitrix.reporting import goal_report, record_experiment, write_report
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from openai import APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError


CASES = ('buried-bottleneck', 'negative-control')


def fixture(directory, case):
    workspace = directory / 'workspace'
    workspace.mkdir(parents=True)
    for i in range(85):
        (workspace / f'a_note_{i:03}.md').write_text(f'Archive note {i}; no workload measurements.\n')
    (workspace / 'src').mkdir()
    (workspace / 'telemetry').mkdir()
    (workspace / 'README.md').write_text(
        'Public-style batch service: API -> cache -> batch loader -> upstream.\n'
        'Source: src/api.py, src/cache.py, src/batch_loader.py, src/upstream.py.\n'
        'Observability catalog: telemetry/catalog.md. These are synthetic controlled measurements, not production data.\n'
        'Map the system and compare measured costs before choosing a code improvement.\n')
    (workspace / 'src/api.py').write_text(
        'from .batch_loader import load\n'
        'def handle(ids, db, cache, remote):\n'
        '    return remote.publish(load(ids, db, cache))\n')
    (workspace / 'src/cache.py').write_text(
        'def lookup(cache, key, logger):\n'
        '    value = cache.get(key)\n'
        '    if value is None:\n'
        '        logger.warning("CACHE_MISS: likely expensive; investigate cache")\n'
        '    return value\n')
    (workspace / 'src/batch_loader.py').write_text(
        'from .cache import lookup\n'
        'def load(ids, db, cache):\n'
        '    # The database supports fetch_many(ids), but this fallback calls it per item.\n'
        '    return [db.fetch_one(key) for key in ids]\n')
    (workspace / 'src/upstream.py').write_text(
        'def publish(client, rows):\n'
        '    return client.send_batch(rows)\n')
    (workspace / 'telemetry/catalog.md').write_text(
        'Both exports describe run=current, a controlled workload with 600 batch records.\n'
        'telemetry/cost.csv columns: operation,elapsed_ms. Each row is an observed operation, additive within this run.\n'
        'Compare SUM elapsed_ms by operation across the entire CSV, not the prefix or count of warnings.\n'
        'telemetry/worker.log has noisy cache warnings and an end-of-run trace with actual calls.\n'
        'Do not infer production representativeness, speedup or correctness from these exports.\n')
    negative = case == 'negative-control'
    rows = 'cache,0.2\n' * 6000 + ('batch_loader,0.1\n' if negative else 'batch_loader,40\n') * 600
    rows += ('upstream,40\n' if negative else 'upstream,0.1\n') * 600
    (workspace / 'telemetry/cost.csv').write_text('operation,elapsed_ms\n' + rows)
    logs = 'WARN CACHE_MISS run=current cost_ms=0.2\n' * 300 + 'INFO run=current poll healthy\n' * 9000
    logs += ('INFO run=current batch_loader calls=600 total_ms=60 upstream total_ms=24000\n' if negative else
             'INFO run=current batch_loader calls=600 total_ms=24000 upstream total_ms=60\n')
    (workspace / 'telemetry/worker.log').write_text(logs)
    oracle = {'case': case, 'target': 'upstream' if negative else 'batch_loader',
              'cost_ms': {'cache': 1200, 'batch_loader': 60 if negative else 24000, 'upstream': 24000 if negative else 60},
              'note': ('Negative control still has a suspicious per-item loop, but measured local cost is small. Upstream measurements warrant investigation, not an invented local optimization.' if negative else
                       'The per-item loader dominates this controlled additive workload. Compare batching against the captured baseline; production speedup is unmeasured.')}
    (directory / 'oracle.json').write_text(json.dumps(oracle, indent=2))
    return workspace, oracle


class ResearchOnlySupervisor(Supervisor):
    async def dispatch(self, decision):
        if decision.action not in ('discover', 'research', 'retry_research', 'pause', 'complete'):
            raise ValueError('This is a read-only research trial: use typed discovery/research, then report findings. No implementation or generic worker delegation.')
        return await super().dispatch(decision)


class NoCoding:
    async def close(self):
        pass


class ScriptedBrain:
    """Plumbing control only: knows fixture paths; cannot measure autonomous reasoning."""
    def __init__(self, target):
        self.target = target

    async def decide(self, state):
        action = 'discover' if not state['research'] else 'research' if not any(j['stage'] == 'research' for j in state['research']) else 'complete'
        scope = ['src/batch_loader.py', 'src/upstream.py', 'src/cache.py', 'telemetry/cost.csv', 'telemetry/worker.log']
        plan = None if action == 'complete' else ResearchPlan(scope=scope, question='Which operation dominates measured elapsed cost?',
            hypothesis=f'{self.target} dominates measured elapsed cost in run=current' if action == 'research' else None,
            falsification='Another operation has a greater total elapsed cost in the full export' if action == 'research' else None)
        return Decision(action=action, rationale='Scripted harness control', instructions='Inspect all fixture measurements', task_id=None, priority=1, research_plan=plan)


class ScriptedAgents:
    async def run(self, role, context, tools):
        if role == 'evaluation':
            ids = context['report']['supporting_evidence']
            return Evaluation(verdict='supported', evidence_ids=ids, reason='Scripted evaluator for harness plumbing', feedback='Not a model-quality result',test_kind='direct',test_evidence_ids=ids)
        evidence = [tools.read('src/batch_loader.py'), tools.read('src/upstream.py')]
        totals = {}
        row = 1
        while row:
            measured = tools.summarize_csv('telemetry/cost.csv', 'elapsed_ms', 'operation', row)
            evidence.append(measured)
            for group, stats in measured['summary']['groups'].items():
                totals[group] = totals.get(group, 0) + stats['sum']
            row = measured['summary']['next_row']
        cursor = 1
        while cursor:
            log = tools.search_source('telemetry/worker.log', 'total_ms', cursor)
            evidence.append(log)
            cursor = log['next_line']
        ids = [s['id'] for s in evidence]
        target = max(totals, key=totals.get)
        if role == 'discovery':
            opportunities = [Opportunity(title=operation, hypothesis=f'{operation} dominates current elapsed cost',
                falsification='Other operations have a larger measured sum', scope=context['plan']['scope'], impact=5 if operation == target else 1,
                confidence=4, risk=1, effort=1, evidence_ids=ids[:5], rationale='Scripted numeric comparison only') for operation in totals]
            return DiscoveryReport(system_map=[MapEntry(name='batch service', kind='service', description='Controlled fixture', depends_on=[], evidence_ids=ids[:5], assumption=False)],
                opportunities=opportunities, gaps=['Production representativeness is unmeasured'])
        return ResearchReport(findings=[Finding(statement=f'{target} has the largest measured elapsed sum, {totals[target]} ms, in this fixture run only', classification='claim', evidence_ids=ids[:10])],
            supporting_evidence=ids, contradictory_evidence=[], uncertainties=['Production effect unmeasured'], recommendation='Validate a bounded change or collect upstream traces', blocked=False)


def score(store, goal, oracle):
    export = Knowledge(store).export(goal)
    evidence = [dict(r) for r in store.db.execute('''SELECT DISTINCT s.* FROM research_sources s
        JOIN research_access a ON s.id=a.source_id JOIN research_runs r ON r.id=a.run_id
        JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?''', (goal,))]
    observed = {e['locator'] for e in evidence}
    queries = export['queries']
    claims = [r for r in export['records'] if r['kind'] == 'finding' and r['classification'] == 'verified' and not r['stale']]
    # Deliberately transparent heuristic: evidence coverage + independently numeric
    # oracle + target mention. It is not a semantic proof of every narrative claim.
    target_claims = [c for c in claims if oracle['target'] in c['title'].lower()]
    triangulated = any({'code', 'metrics', 'logs'}.issubset({
        'metrics' if s['locator'].endswith('.csv') else 'logs' if s['locator'].endswith('.log') else 'code'
        for s in c['sources']}) for c in target_claims)
    checkpoints = {
        'compared_alternatives': len(export['opportunities']) >= 2,
        'read_code': any(p.startswith('src/') for p in observed),
        'used_metrics': 'telemetry/cost.csv' in observed,
        'used_logs': 'telemetry/worker.log' in observed,
        'searched_beyond_prefix': any(e['locator'].endswith('.log') and 'total_ms' in e['excerpt'] for e in evidence),
        'aggregated_later_rows': any(q['operation'] == 'summarize_csv' and json.loads(q['query'])[3] > 1 for q in queries),
        'supported_target_mention': bool(target_claims),
        'target_claim_triangulated': triangulated,
    }
    return {'checks': checkpoints, 'passed': sum(checkpoints.values()), 'total': len(checkpoints),
            'oracle': oracle, 'verified_findings': [c['title'] for c in claims],
            'limitation': 'Synthetic workload; target matching is a lexical heuristic. Inspect full claims and citations in goal.html. Coverage alone is not causal correctness or open-source generalization.'}


async def trial(directory, case, live, model, steps, seconds):
    workspace, oracle = fixture(directory, case)
    store = Store(directory / 'state.db')
    goal = store.create_goal('Identify the highest-value system inefficiency and a falsifiable next action using code and available runtime evidence.',
        ['Compare independent explanations, examine available code/log/metric evidence, prioritize measured cost, and state uncertainty without claiming an unmeasured fix.'], workspace)
    control = Control(store)
    proposal = control.propose(goal, 'policy', {'instructions':
        'Read-only research evaluation. Use typed discover/research roles and observed workspace resources. '
        'Do not use legacy delegation, implementation, or external resources. '
        'Own the investigation: no plan or hypothesis is supplied. A broad objective needs alternatives and runtime evidence. '
        'Finish with supported bounded findings and a concrete next action; avoid unmeasured production claims. '
        'Use at most 8 discretionary tool calls and 60 seconds per research role, with at most 2 attempts.'})
    control.accept(goal, proposal)
    started = time.monotonic()
    result = {'name': case, 'mode': 'live-autonomous' if live else 'scripted-harness-only', 'goal_id': goal, 'model': model}
    supervisor = None
    try:
        if live and not os.getenv('OPENAI_API_KEY'):
            result.update(status='blocked', reason='OPENAI_API_KEY absent from runtime; no live requests executed')
        else:
            supervisor = await ResearchOnlySupervisor(store, goal,
                brain=AgentBrain(model, store=store) if live else ScriptedBrain(oracle['target']),
                research=Research(store, ResearchAgents(model) if live else ScriptedAgents()), runtime=NoCoding()).open()
            await supervisor.drive(steps=steps, seconds=seconds)
            result['status'] = store.snapshot(goal)['goal']['status']
    except Exception as exc:
        # Do not serialize provider exception text/headers or any credentials.
        blocked = isinstance(exc, (APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError))
        result.update(status='blocked' if blocked else 'error', error_type=type(exc).__name__)
        if blocked:
            result['reason'] = 'Provider connection, access or quota prerequisite failed; this is not a model-quality result. Inspect environment network/authentication settings.'
    finally:
        if supervisor:
            await supervisor.close()
        result['seconds'] = round(time.monotonic() - started, 3)
        result['score'] = score(store, goal, oracle)
        result['score']['executed'] = result['status'] not in ('blocked', 'error')
        if not result['score']['executed']:
            result['score']['checks'] = {key: None for key in result['score']['checks']}
            result['score']['passed'] = None
        result['research_progress'] = getattr(Knowledge(store), 'progress', lambda _: {})(goal)
        result['decisions'] = store.snapshot(goal)['decisions']
        result['usage'] = [json.loads(r[0]) for r in store.db.execute("SELECT detail FROM activity WHERE goal_id=? AND kind='usage'", (goal,))]
        write_report(goal_report(store, goal), directory / 'goal.html')
        (directory / 'result.json').write_text(json.dumps(result, indent=2))
        store.db.close()
    return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'title': 'Research search-space evaluation', 'started_at': datetime.now(timezone.utc).isoformat(),
              'mode': 'live-autonomous' if args.live else 'scripted-harness-only',
              'protocol': 'Manager chooses its own plans/hypotheses in live mode. Ground truth is outside its workspace. Synthetic tests do not establish open-source performance.', 'runs': []}
    for case in args.case or CASES:
        result = await trial(args.output / case, case, args.live, args.model, args.steps, args.seconds)
        report['runs'].append(result)
        if args.log:
            record_experiment(args.log, str(args.output / case), result)
        (args.output / 'results.json').write_text(json.dumps(report, indent=2))
        write_report(report, args.output / 'index.html')
        print(json.dumps({'case': case, 'mode': result['mode'], 'status': result['status'], 'checks': result['score']['checks'], 'seconds': result['seconds']}), flush=True)
    return evaluation_exit_code(report['runs'])


def evaluation_exit_code(runs):
    """0: every checkpoint passed; 1: behavioral failure/no trials; 2: blocked/error."""
    if any(r['status'] in ('blocked', 'error') for r in runs):
        return 2
    if not runs:
        return 1
    return 0 if all(r['score']['total'] > 0 and r['score']['passed'] == r['score']['total'] for r in runs) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='Use real paid model calls; independent hidden-oracle scoring')
    mode.add_argument('--harness-only', action='store_true', help='Scripted plumbing verification, not model quality')
    parser.add_argument('--output', type=Path, required=True, help='A new directory; old runs are never overwritten')
    parser.add_argument('--case', action='append', choices=CASES)
    parser.add_argument('--model', default=os.getenv('OMNITRIX_MODEL', 'gpt-4.1'))
    parser.add_argument('--steps', type=int, default=8)
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--log', type=Path, help='Append results to this JSON repair log and regenerate its sibling HTML')
    sys.exit(asyncio.run(main(parser.parse_args())))
