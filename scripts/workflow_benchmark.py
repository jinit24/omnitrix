#!/usr/bin/env python3
"""Offline control-plane benchmark: real storage/tools, deterministic specialists.

No model or external worker is called. This verifies orchestration invariants,
not finding quality, live-model cost savings, or architectural superiority.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.exceptions import ModelBehaviorError
from omnitrix.audit import Audit
from omnitrix.budget import Budget
from omnitrix.campaign import Campaign
from omnitrix.control import Control
from omnitrix.execution import Execution
from omnitrix.knowledge import ResearchTools
from omnitrix.manager_context import (CONTEXT_BYTES, HISTORY_BYTES, encoded,
                                     manager_context, manager_history_filter)
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import Evaluation, Finding, ResearchPlan, ResearchReport
from omnitrix.scheduling import enqueue_batch
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor

FIXTURES = {
    'sources/ci/wrapper.sh': '#!/bin/bash\nsource helper.sh\nstatus=0\nrun_checks\nexit "$status"\n',
    'sources/ci/helper.sh': 'run_checks() {\n  false\n  status=$?\n}\n',
    'sources/auth/handler.py': 'from contract import permitted\ndef handle(user):\n    return permitted(user)\n',
    'sources/auth/contract.py': 'def permitted(user):\n    return user == "admin"\n',
    'sources/storage/reader.py': 'from contract import expected\ndef read(value):\n    return value == expected\n',
    'sources/storage/contract.py': 'expected = "durable"\n',
    'sources/network/unexamined.txt': 'Unexamined network trust boundary; do not claim complete coverage.\n',
}
TASKS = (
    ('ci', 'wrapper.sh', 'helper.sh', 'The wrapper preserves the helper failure status'),
    ('auth', 'handler.py', 'contract.py', 'The handler permits only the admin fixture user'),
    ('storage', 'reader.py', 'contract.py', 'The reader checks the durable fixture value'),
)
RUBRIC = 'Inspect the dependency and cite current source evidence'
POLICY = {'version': 1, 'tasks': TASKS, 'rubric': RUBRIC, 'mode': 'scripted-control-plane-only',
          'faults': ['malformed-decision', 'process-death-record', 'stale-source', 'outside-root', 'premature-completion']}


def digest(value):
    data = value if isinstance(value, bytes) else encoded(value)
    return hashlib.sha256(data).hexdigest()


def frozen_metadata():
    files = sorted([*ROOT.joinpath('omnitrix').glob('*.py'), *ROOT.joinpath('instructions').glob('*.md'), Path(__file__)])
    hashes = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in files}
    return {'code_sha256': digest(hashes), 'files_sha256': hashes, 'fixture_sha256': digest(FIXTURES),
            'scripted_policy_sha256': digest(POLICY), 'scripted_policy': json.loads(json.dumps(POLICY)),
            'models': [], 'provider_calls': 0, 'external_worker_calls': 0}


class NoExternalRuntime:
    on_event = None

    async def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError(f'Offline benchmark attempted external runtime operation: {name}')


class NoBrain:
    calls = 0

    async def decide(self, snapshot):
        self.calls += 1
        raise AssertionError('Recovery or approved dispatch unexpectedly called a manager')


def fixture(directory):
    directory.mkdir(parents=True)
    workspace = directory / 'repo'
    for locator, content in FIXTURES.items():
        path = workspace / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    store = Store(directory / 'state.sqlite')
    goal = store.create_goal('Resolve bounded repository contracts and retain all audit gaps',
                             ['Do not assert exhaustive coverage from source sampling'], workspace)
    control = Control(store)
    control.initialize(goal)
    control.accept(goal, control.propose(goal, 'research_context', {
        'brief': 'Resolve the stated fixture question using implementation and dependency evidence.',
        'rubric': [RUBRIC], 'require_checkpoint': True, 'require_evidence_plan': True}))
    Audit(store, goal).initialize([
        {'path': area, 'files': sum(path.startswith(f'sources/{area}/') for path in FIXTURES),
         'bytes': sum(len(body.encode()) for path, body in FIXTURES.items() if path.startswith(f'sources/{area}/')),
         'kind': 'offline fixture area'} for area in ('ci', 'auth', 'storage', 'network')])
    return store, goal, workspace


def plan(task):
    area, entry, dependency, hypothesis = task
    locator = f'sources/{area}/{entry}'
    length = len(FIXTURES[locator].splitlines())
    return ResearchPlan(primary_area=area, scope=[locator], question=f'Does current source establish: {hypothesis}?',
        hypothesis=hypothesis, falsification='The actual callee or final outcome contradicts the declared fixture behavior',
        decision='Decide whether this bounded contract is supported', expected_value='Prevent a speculative finding',
        stop_condition='Current entry and callee source read, checkpoint saved, independent review complete',
        evidence_obligations=[{'kind': 'implementation', 'locator': locator, 'start_line': 1, 'end_line': length},
                              {'kind': 'final_outcome', 'locator': locator, 'start_line': length, 'end_line': length}],
        unresolved_dependencies=[{'kind': 'callee', 'symbol': dependency, 'required': False,
                                  'reason': 'Resolve the discovered prerequisite using a bounded same-attempt scope grant'}],
        allow_scope_expansion=True, dependency_roots=[f'sources/{area}'],
        budget={'tool_calls': 6, 'seconds': 10, 'attempts': 1})


def decision(action, **kwargs):
    return Decision(action=action, rationale='Deterministic fixture policy', instructions='Inspect current source and preserve gaps',
                    task_id=None, priority=1, **kwargs)


class ScriptedSpecialists:
    """Fixed observations are intentionally not evidence of model intelligence."""
    def __init__(self):
        self.calls = []

    async def run(self, role, context, tools):
        self.calls.append(role)
        if role == 'evaluation':
            # Independent role uses the real bounded read tool, not just report IDs.
            for locator in context['final_scope']:
                tools.read(locator)
            ids = context['report']['supporting_evidence']
            return Evaluation(verdict='supported', evidence_ids=ids,
                rubric_assessments=[{'criterion': RUBRIC, 'verdict': 'met',
                                    'reason': 'The deterministic reviewer independently read both explicit fixture sources', 'evidence_ids': ids}],
                reason='Scripted fixture observation agrees with both captured sources', feedback='Retain unexamined boundaries')
        entry = tools.read(context['plan']['scope'][0])
        task = next(task for task in TASKS if task[0] == context['plan']['primary_area'])
        dependency = f'sources/{task[0]}/{task[2]}'
        tools.request_dependency([entry['id']], 'The captured entry calls this prerequisite; inspect its exact bounded body',
            [{'locator': dependency, 'start_line': 1, 'end_line': len(FIXTURES[dependency].splitlines())}])
        callee = tools.read(dependency)
        ids = [entry['id'], callee['id']]
        tools.checkpoint(ids, 'Static fixture only; real runtime behavior remains unmeasured', 'Return the bounded result for independent review')
        return ResearchReport(stop_reason='resolved', findings=[Finding(statement=task[3], classification='claim', evidence_ids=ids)],
            supporting_evidence=ids, contradictory_evidence=[], uncertainties=['No runtime or production claim'],
            recommendation='Retain this narrow contract and continue the unexamined audit frontier', blocked=False)


class StrategyBrain:
    def __init__(self, batch):
        self.batch, self.calls = batch, 0

    async def decide(self, snapshot):
        self.calls += 1
        if self.batch:
            if self.calls != 1:
                raise AssertionError('Approved batch asked for a repeated strategy decision')
            return decision('schedule_research', research_batch=[{'stage': 'research', 'plan': plan(task), 'priority': 1} for task in TASKS])
        return decision('research', research_plan=plan(TASKS[self.calls - 1]))


async def dispatch_arm(directory, batch):
    store, goal, workspace = fixture(directory)
    specialists, brain = ScriptedSpecialists(), StrategyBrain(batch)
    supervisor = await Supervisor(store, goal, brain=brain, runtime=NoExternalRuntime(),
                                  research=Research(store, specialists)).open()
    try:
        if batch:
            scheduled = await supervisor.advance(steps=1)
            assert len(scheduled['scheduled']) == len(TASKS), 'Strategy did not durably enqueue every approved plan'
        for _ in TASKS:
            started = await supervisor.advance(steps=1)
            assert 'task_id' in started, f'Fixture dispatch failed: {started}'
            await asyncio.gather(supervisor.work[started['task_id']])
        jobs = [dict(row) for row in store.db.execute('SELECT id,status,attempts FROM research_jobs ORDER BY created')]
        assert len(jobs) == len(TASKS) and all(row['status'] == 'supported' and row['attempts'] == 1 for row in jobs), jobs
        assert specialists.calls.count('research') == specialists.calls.count('evaluation') == len(TASKS)
        assert store.db.execute('SELECT count(*) FROM research_scope_grants').fetchone()[0] == len(TASKS)
        assert store.db.execute("SELECT count(*) FROM research_progress WHERE kind='checkpoint'").fetchone()[0] == len(TASKS)
        assert brain.calls == (1 if batch else len(TASKS))
        if batch:
            assert Campaign(store, goal).summary()['queue']['completed'] == len(TASKS)
        # Current evaluated source samples still leave the network boundary open.
        try:
            await supervisor.dispatch(decision('complete'))
        except ValueError as exc:
            assert 'incomplete' in str(exc).lower()
        else:
            raise AssertionError('Premature repository completion was accepted')
        assert store.db.execute('SELECT status FROM goals WHERE id=?', (goal,)).fetchone()[0] != 'completed'
        return {'manager_strategy_calls': brain.calls, 'scripted_specialist_calls': len(specialists.calls),
                'supported_bounded_contracts': len(jobs), 'attempts': sum(row['attempts'] for row in jobs),
                'same_attempt_scope_grants': len(TASKS), 'premature_completion_rejected': True,
                'remaining_areas': Audit(store, goal).completion_gaps(), 'database': str(store.path.relative_to(directory.parent)),
                'sqlite_integrity': store.db.execute('PRAGMA integrity_check').fetchone()[0]}
    finally:
        await supervisor.close()
        store.db.close()


async def batch_comparison(directory):
    batch = await dispatch_arm(directory / 'batch', True)
    baseline = await dispatch_arm(directory / 'per-task', False)
    assert batch['supported_bounded_contracts'] == baseline['supported_bounded_contracts']
    return {'batch': batch, 'per_task_scripted_baseline': baseline,
            'interpretation': 'Dispatch-call reduction is deterministic for already-approved work; no live token, dollar, or finding-quality improvement is established.'}


async def schema_repair(directory):
    outcomes = []
    for fail_repair in (False, True):
        store, goal, _ = fixture(directory / ('persistent-error' if fail_repair else 'repair-success'))
        class Brain:
            calls = 0
            async def decide(self, snapshot):
                self.calls += 1
                if self.calls == 1 or fail_repair:
                    raise ModelBehaviorError('Injected invalid structured decision JSON')
                assert snapshot['manager_repair']['category'] == 'structured_output'
                assert snapshot['manager_repair']['fresh_context']
                return decision('pause')
        brain = Brain()
        supervisor = await Supervisor(store, goal, brain=brain, runtime=NoExternalRuntime()).open()
        try:
            result = await supervisor.advance(steps=2)
            state = store.db.execute('SELECT state FROM manager_repair_tickets').fetchone()[0]
            assert brain.calls == 2 and supervisor.autonomy == 0
            assert state == ('failed' if fail_repair else 'completed')
            assert bool(result.get('blocked')) == fail_repair
            outcomes.append({'fault_persists': fail_repair, 'manager_calls': brain.calls, 'repair_state': state,
                             'dispatched_workers': store.db.execute('SELECT count(*) FROM tasks').fetchone()[0]})
        finally:
            await supervisor.close()
            store.db.close()
    return {'cases': outcomes, 'automatic_repair_limit': 1}


async def restart_reconciliation(directory):
    store, goal, workspace = fixture(directory)
    job = Research(store).plan(goal, 'research', plan(TASKS[0]))['job_id']
    now = time.time()
    store.execute("UPDATE research_jobs SET status='running',attempts=1 WHERE id=?", (job,))
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('interrupted-run',?,'running',?)", (job, now))
    tools = ResearchTools(store, 'interrupted-run', str(workspace), [f'sources/ci/{TASKS[0][1]}'], 6)
    evidence = tools.read(f'sources/ci/{TASKS[0][1]}')
    tools.checkpoint([evidence['id']], 'Prerequisite not read before simulated process death', 'Reconcile the retained observation')
    task = Execution(store, type('LocalAdapter', (), {'name': 'research'})()).assign(goal, 'Local read-only observation', 1)
    store.execute('''INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,owner,started,last_progress,deadline)
                     VALUES(?,1,1,'running',?,'dead-process',?,?,?)''', (task, json.dumps({'job_id': job}), now, now, now + 30))
    budget = Budget.configure(store, goal, 25, {'fixture': {'input_per_million': 5, 'output_per_million': 15}})
    budget.reserve(encoded({'model': 'fixture', 'input': 'Uncertain retained request', 'max_output_tokens': 10, 'service_tier': 'default'}), 'research')
    before = budget.summary()
    path = store.path
    store.db.close()
    reopened = Store(path)
    brain = NoBrain()
    supervisor = await Supervisor(reopened, goal, brain=brain, runtime=NoExternalRuntime()).open()
    try:
        after = Budget(reopened, goal).summary()
        state = reopened.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0]
        assert state == 'reconciled' and brain.calls == 0
        assert supervisor.research.job(job)['attempts'] == 1
        assert after['requests'] == before['requests'] and after['committed_usd'] == before['committed_usd']
        assert reopened.db.execute('SELECT count(*) FROM research_access').fetchone()[0] == 1
        assert reopened.db.execute("SELECT count(*) FROM messages WHERE kind='reconciled_outcome'").fetchone()[0] == 1
        return {'runtime_state': state, 'manager_calls': brain.calls, 'preserved_attempts': 1,
                'evidence_preserved': True, 'requests_before': before['requests'], 'requests_after': after['requests'],
                'committed_usd_before': before['committed_usd'], 'committed_usd_after': after['committed_usd'],
                'sqlite_integrity': reopened.db.execute('PRAGMA integrity_check').fetchone()[0],
                'fault': 'A real SQLite process-death record, closed connection and reopened supervisor; not an OS kill test'}
    finally:
        await supervisor.close()
        reopened.db.close()


async def scope_fences(directory):
    store, goal, workspace = fixture(directory)
    try:
        enqueue_batch(store, goal, [{'stage': 'research', 'plan': plan(TASKS[0]), 'priority': 1}])
        (workspace / 'sources/ci/wrapper.sh').write_text('Changed source revision\n')
        assert Campaign(store, goal).claim_next() is None
        assert Campaign(store, goal).summary()['queue']['invalidated'] == 1
        # A different untouched job provides current captured callsite evidence.
        job = Research(store).plan(goal, 'research', plan(TASKS[1]))['job_id']
        store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('root-fence',?,'running',?)", (job, time.time()))
        tools = ResearchTools(store, 'root-fence', str(workspace), ['sources/auth/handler.py'], 6,
                              allow_scope_expansion=True, dependency_roots=['sources/auth'])
        source = tools.read('sources/auth/handler.py')
        try:
            tools.request_dependency([source['id']], 'Attempt an unauthorized unrelated prerequisite',
                                     [{'locator': 'sources/storage/contract.py', 'start_line': 1, 'end_line': 1}])
        except ValueError as exc:
            assert 'approved dependency root' in str(exc)
        else:
            raise AssertionError('Dependency outside approved roots was granted')
        assert tools.grants() == [] and tools.scope == {'sources/auth/handler.py'}
        return {'stale_queued_source_invalidated': True, 'outside_root_dependency_rejected': True,
                'unauthorized_scope_grants': 0}
    finally:
        store.db.close()


async def atomic_preflight_admission(directory):
    store, goal, _ = fixture(directory)
    try:
        invalid = plan(TASKS[1]).model_dump()
        invalid['evidence_obligations'].append({'kind': 'callee', 'locator': 'sources/auth/contract.py',
                                                 'start_line': 1, 'end_line': 2, 'required': True})
        try:
            enqueue_batch(store, goal, [{'stage': 'research', 'plan': plan(TASKS[0])},
                                       {'stage': 'research', 'plan': invalid}])
        except ValueError as exc:
            assert 'outside scope' in str(exc)
        else:
            raise AssertionError('Batch with a missing required callee was admitted')
        assert store.db.execute('SELECT count(*) FROM research_jobs').fetchone()[0] == 0
        assert Campaign(store, goal).summary()['queued_jobs'] == 0
        infeasible = plan(TASKS[0]).model_dump()
        infeasible['budget']['tool_calls'] = 1
        try:
            Research(store).preflight(goal, 'research', infeasible)
        except ValueError as exc:
            assert 'Infeasible research tool budget' in str(exc)
        else:
            raise AssertionError('Plan exhausted the mandatory checkpoint allowance before dispatch')
        assert store.db.execute('SELECT count(*) FROM research_runs').fetchone()[0] == 0
        return {'missing_required_callee_rejected': True, 'infeasible_read_and_checkpoint_budget_rejected': True,
                'partial_batch_jobs': 0, 'worker_starts': 0, 'provider_calls': 0}
    finally:
        store.db.close()


async def context_compaction(directory):
    store, goal, _ = fixture(directory)
    try:
        # Persist oversized fixture handoffs: compare equal count-limited inputs.
        for index in range(3):
            Control(store).message(goal, 'findings', {'job_id': f'synthetic-{index}', 'status': 'insufficient',
                'report': {'findings': [{'statement': 'Long synthetic finding ' * 1000, 'classification': 'claim', 'evidence_ids': []}],
                           'uncertainties': ['Missing callee ' * 1000]},
                'evaluation': {'verdict': 'insufficient', 'reason': 'Need actual contract ' * 1000}}, direction='worker_to_manager')
        snapshot = store.snapshot(goal)
        count_limited = {key: snapshot[key] for key in ('goal', 'research_context', 'worker_messages')}
        count_limited['worker_messages'] = count_limited['worker_messages'][:3]
        compact = manager_context(snapshot)
        assert len(encoded(compact)) <= CONTEXT_BYTES and len(encoded(compact)) < len(encoded(count_limited))
        assert compact['goal'] == snapshot['goal'] and compact['research_context'] == snapshot['research_context']
        current = {'role': 'user', 'content': json.dumps(compact)}
        history = []
        for index in range(16):
            history.extend([{'type': 'function_call', 'name': 'retrieve_record', 'arguments': '{}', 'call_id': str(index)},
                            {'type': 'function_call_output', 'call_id': str(index), 'output': json.dumps({'body': 'x' * 20000})}])
        filtered = manager_history_filter(SimpleNamespace(model_data=SimpleNamespace(input=[current, *history], instructions='Required fixture policy')))
        retained = [item for item in filtered.input if item != current]
        assert current in filtered.input and len(encoded(retained)) <= HISTORY_BYTES
        assert {item['call_id'] for item in retained if item.get('type') == 'function_call'} == {
            item['call_id'] for item in retained if item.get('type') == 'function_call_output'}
        return {'synthetic_handoff_count': 3, 'count_limited_snapshot_bytes': len(encoded(count_limited)),
                'compact_snapshot_bytes': len(encoded(compact)), 'snapshot_byte_ceiling': CONTEXT_BYTES,
                'filtered_history_bytes': len(encoded(retained)), 'history_byte_ceiling': HISTORY_BYTES,
                'mandatory_contract_preserved': True, 'complete_tool_pairs': True,
                'interpretation': 'Synthetic serialization measurement; no live API token or quality savings established.'}
    finally:
        store.db.close()


SCENARIOS = (
    ('approved_batch_and_dependency_repair', batch_comparison),
    ('bounded_structured_output_repair', schema_repair),
    ('read_only_restart_reconciliation', restart_reconciliation),
    ('stale_source_and_scope_fences', scope_fences),
    ('atomic_scope_preflight', atomic_preflight_admission),
    ('bounded_manager_context', context_compaction),
)


async def run_benchmark(output):
    output = Path(output).resolve()
    # Never silently reuse partially executed fault fixtures or replace evidence.
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'schema_version': 1, 'mode': 'deterministic-control-plane-only', 'frozen': frozen_metadata(), 'scenarios': [],
              'limitations': ['No live models or public repository audits were run.',
                             'Scripted specialists have known answers; finding quality and architecture superiority are not measured.',
                             'Strategy-call and synthetic byte comparisons are not measured dollar savings.',
                             'Recovery closes and reopens a real database with interrupted records; this is not a new OS crash experiment.']}
    for name, operation in SCENARIOS:
        before = time.monotonic()
        try:
            measurements = await operation(output / name)
            result = {'name': name, 'passed': True, 'measurements': measurements}
        except Exception as exc:
            result = {'name': name, 'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
        result['wall_seconds'] = time.monotonic() - before
        report['scenarios'].append(result)
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    report['frozen_code_unchanged'] = frozen_metadata()['code_sha256'] == report['frozen']['code_sha256']
    report['passed'] = all(row['passed'] for row in report['scenarios']) and report['frozen_code_unchanged']
    report['wall_seconds'] = time.monotonic() - started
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory for durable databases and result.json')
    args = parser.parse_args()
    try:
        report = asyncio.run(run_benchmark(args.output))
    except FileExistsError:
        parser.error('Output already exists; choose a new directory to preserve previous evidence')
    print(json.dumps({'passed': report['passed'], 'mode': report['mode'], 'scenarios': len(report['scenarios']),
                      'provider_calls': 0, 'report': str(args.output.resolve() / 'result.json')}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
