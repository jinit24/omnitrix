"""Failure injection for local recovery and manager-free approved dispatch."""
import asyncio
import json
import time

import pytest
from agents.exceptions import ModelBehaviorError

from omnitrix.budget import Budget, BudgetError
from omnitrix.control import Control
from omnitrix.execution import Execution
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from tests_helpers import FixtureAgents


class LocalRuntime:
    async def close(self):
        pass

    async def reconcile(self, thread):
        pytest.fail('Local recovery must not contact external runtime')


@pytest.fixture
def system(tmp_path):
    (tmp_path / 'source.md').write_text('Service queue uses polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit the polling contract', ['Retain evidence'], tmp_path)
    Control(store).initialize(goal)
    return store, goal, tmp_path


def stop_decision():
    return Decision(action='pause', rationale='Report retained results', instructions='Keep gaps explicit', task_id=None, priority=1)


def focused(question='What is the polling interval?'):
    return ResearchPlan(scope=['source.md'], question=question, hypothesis='Polling interval is 30 seconds',
                        falsification='Source specifies a different interval')


def reserve(store, goal):
    budget = Budget.configure(store, goal, 25, {'fixture': {'input_per_million': 5, 'output_per_million': 15}})
    budget.reserve(json.dumps({'model': 'fixture', 'input': 'retained request', 'max_output_tokens': 10,
                               'service_tier': 'default'}).encode(), 'research')
    return budget.summary()


async def test_open_reconciles_only_local_observations_without_resetting_spend(system):
    store, goal, _ = system
    research = Research(store, FixtureAgents())
    job = research.plan(goal, 'research', focused())['job_id']
    store.execute("UPDATE research_jobs SET status='running',attempts=1 WHERE id=?", (job,))
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('old-run',?,'running',?)", (job, time.time()))
    task = Execution(store, type('Adapter', (), {'name': 'research'})()).assign(goal, 'Local research', 1)
    now = time.time()
    store.execute("INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,owner,started,last_progress,deadline) VALUES(?,1,1,'running',?,'old-owner',?,?,?)",
                  (task, json.dumps({'job_id': job}), now, now, now + 300))
    before = reserve(store, goal)

    class Brain:
        async def decide(self, snapshot):
            pytest.fail('Opening recovery must not call a model')

    supervisor = await Supervisor(Store(store.path), goal, brain=Brain(), runtime=LocalRuntime()).open()
    try:
        assert supervisor.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == 'reconciled'
        assert supervisor.research.job(job)['attempts'] == 1
        assert supervisor.db.execute("SELECT status FROM research_runs WHERE id='old-run'").fetchone()[0] == 'interrupted'
        after = Budget(supervisor.store, goal).summary()
        assert after['committed_usd'] == before['committed_usd']
        assert after['requests'] == before['requests']
        assert supervisor.db.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='reconciled_outcome'", (task,)).fetchone()
    finally:
        await supervisor.close()


async def test_restart_releases_interrupted_additional_review_escrow_without_refunding_requests(system):
    store, goal, _ = system
    job = Research(store, FixtureAgents()).plan(goal, 'research', focused())['job_id']
    now = time.time()
    store.execute("UPDATE research_jobs SET status='insufficient',attempts=1 WHERE id=?", (job,))
    store.execute("INSERT INTO research_runs(id,job_id,status,created,finished) VALUES('original-run',?,'insufficient',?,?)",
                  (job, now - 2, now - 1))
    store.execute("""INSERT INTO research_reviews(id,job_id,run_id,feedback,context,revisions,status,created)
                     VALUES('additional-review',?,'original-run','Inspect retained proof','{}','{}','running',?)""", (job, now))
    task = Execution(store, type('Adapter', (), {'name': 'evaluation'})()).assign(goal, 'Additional local review', 1)
    store.execute("INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,owner,started,last_progress,deadline) VALUES(?,1,1,'running',?,'old-owner',?,?,?)",
                  (task, json.dumps({'job_id': job, 'review_only': True}), now, now, now + 300))
    budget = Budget.configure(store, goal, 25, {'fixture': {'input_per_million': 5, 'output_per_million': 15}})
    budget.admit_review('additional-review', 1.5)
    request = budget.reserve(json.dumps({'model': 'fixture', 'input': 'retained evaluation request',
        'max_output_tokens': 10, 'service_tier': 'default'}).encode(), 'evaluation', review_id='additional-review')
    budget.uncertain(request, 'Process stopped before a measured response')
    before = budget.summary()
    assert before['protected_review_usd'] > 0

    supervisor = await Supervisor(Store(store.path), goal, runtime=LocalRuntime()).open()
    try:
        assert supervisor.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == 'reconciled'
        assert supervisor.db.execute("SELECT status FROM research_reviews WHERE id='additional-review'").fetchone()[0] == 'interrupted'
        after = Budget(supervisor.store, goal).summary()
        assert after['protected_review_usd'] == 0
        assert after['committed_usd'] == before['committed_usd']
        assert after['requests'] == before['requests'] == 1
        assert after['unresolved_requests'] == before['unresolved_requests'] == 1
        assert supervisor.db.execute("SELECT state FROM dollar_requests WHERE id=?", (request,)).fetchone()[0] == 'uncertain'
    finally:
        await supervisor.close()


async def test_external_adapter_cannot_masquerade_as_local_research(system):
    store, goal, _ = system
    job = Research(store, FixtureAgents()).plan(goal, 'research', focused())['job_id']
    task = Execution(store, type('Adapter', (), {'name': 'codex'})()).assign(goal, 'Unknown external start', 1)
    now = time.time()
    store.execute("INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,owner,started,last_progress,deadline) VALUES(?,1,1,'running',?,'old-owner',?,?,?)",
                  (task, json.dumps({'job_id': job}), now, now, now + 300))
    supervisor = await Supervisor(store, goal, runtime=LocalRuntime()).open()
    try:
        assert store.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == 'uncertain'
        assert (await supervisor.reconcile(task))['blocked']
        assert (await supervisor.advance(steps=1))['blocked']
    finally:
        await supervisor.close()


async def test_structured_failure_gets_one_changed_corrective_decision(system):
    store, goal, _ = system

    class Brain:
        def __init__(self):
            self.snapshots = []

        async def decide(self, snapshot):
            self.snapshots.append(snapshot)
            if len(self.snapshots) == 1:
                raise ModelBehaviorError('Invalid JSON for structured decision schema')
            assert snapshot['manager_repair']['fresh_context']
            assert snapshot['manager_repair']['category'] == 'structured_output'
            return stop_decision()

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=LocalRuntime()).open()
    try:
        result = await supervisor.advance(steps=2)
        assert result['action'] == 'pause'
        assert len(brain.snapshots) == 2 and supervisor.autonomy == 0
        assert store.db.execute('SELECT state FROM manager_repair_tickets').fetchone()[0] == 'completed'
        assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 1
    finally:
        await supervisor.close()


async def test_repair_failure_is_bounded_and_original_reservation_retained(system):
    store, goal, _ = system
    before = reserve(store, goal)

    class Brain:
        calls = 0

        async def decide(self, snapshot):
            self.calls += 1
            raise ModelBehaviorError('Invalid JSON for decision schema')

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=LocalRuntime()).open()
    try:
        result = await supervisor.advance(steps=8)
        assert result['blocked']['error'] == 'ModelBehaviorError'
        assert brain.calls == 2 and supervisor.autonomy == 0
        assert store.db.execute('SELECT state FROM manager_repair_tickets').fetchone()[0] == 'failed'
        await supervisor.advance(steps=8)
        assert brain.calls == 3  # Explicit continuation cannot buy another repair for this revision.
        assert store.db.execute('SELECT count(*) FROM manager_repair_tickets').fetchone()[0] == 1
        assert Budget(store, goal).summary()['committed_usd'] == before['committed_usd']
    finally:
        await supervisor.close()


async def test_rejected_history_gets_fresh_request_without_releasing_uncertainty(system):
    store, goal, _ = system
    before = reserve(store, goal)

    class Brain:
        calls = 0

        async def decide(self, snapshot):
            self.calls += 1
            if self.calls == 1:
                raise BudgetError('Provider HTTP 400: No tool call found for function call output; outcome uncertain and reservation retained')
            assert snapshot['manager_repair']['category'] == 'history_pairing'
            assert snapshot['manager_repair']['fresh_context']
            return stop_decision()

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=LocalRuntime()).open()
    try:
        assert (await supervisor.advance(steps=2))['action'] == 'pause'
        assert brain.calls == 2
        after = Budget(store, goal).summary()
        assert after['committed_usd'] == before['committed_usd']
        assert after['unresolved_requests'] == before['unresolved_requests']
        assert store.db.execute('SELECT category,state FROM manager_repair_tickets').fetchone()['state'] == 'completed'
    finally:
        await supervisor.close()


async def test_terminal_budget_failure_remains_paused_without_replay(system):
    store, goal, _ = system

    class Brain:
        calls = 0

        async def decide(self, snapshot):
            self.calls += 1
            raise BudgetError('Dollar budget exhausted: next request exceeds the immutable cap')

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=LocalRuntime()).open()
    try:
        assert (await supervisor.advance(steps=5))['blocked']
        assert (await supervisor.advance(steps=5))['blocked']
        assert brain.calls == 1
        assert not store.db.execute('SELECT 1 FROM manager_repair_tickets').fetchone()
    finally:
        await supervisor.close()


async def test_context_limit_gets_fresh_context_but_no_extra_autonomy(system):
    from omnitrix.manager_context import ContextLimitError
    store, goal, _ = system

    class Brain:
        calls = 0

        async def decide(self, snapshot):
            self.calls += 1
            if self.calls == 1:
                raise ContextLimitError('Assembled manager input exceeds the byte bound')
            assert snapshot['manager_repair']['category'] == 'context_limit'
            return stop_decision()

    brain = Brain()
    supervisor = await Supervisor(store, goal, brain=brain, runtime=LocalRuntime()).open()
    try:
        assert (await supervisor.advance(steps=1))['blocked']
        assert brain.calls == 1
        assert not store.db.execute('SELECT 1 FROM manager_repair_tickets').fetchone()
    finally:
        await supervisor.close()


async def test_interrupted_corrective_request_stays_paused_on_open(system):
    store, goal, _ = system
    supervisor = Supervisor(store, goal, runtime=LocalRuntime())
    store.execute("INSERT INTO manager_repair_tickets VALUES('pending-repair',?,'fingerprint','structured_output','invalid json','pending',?,NULL)", (goal, time.time()))
    await supervisor.open()
    try:
        assert store.db.execute('SELECT state FROM manager_repair_tickets').fetchone()[0] == 'interrupted'
        assert store.db.execute('SELECT status FROM goals WHERE id=?', (goal,)).fetchone()[0] == 'paused'
        assert (await supervisor.advance(steps=5))['blocked']
    finally:
        await supervisor.close()


async def test_queue_executes_approved_research_without_manager_call(system):
    store, goal, _ = system

    class Brain:
        async def decide(self, snapshot):
            pytest.fail('An approved current queue entry must not need another manager call')

    supervisor = await Supervisor(store, goal, brain=Brain(), runtime=LocalRuntime(),
                                  research=Research(store, FixtureAgents())).open()
    try:
        decision = Decision(action='schedule_research', rationale='Resolve polling contract', instructions='Read the evidence',
            task_id=None, priority=1, research_batch=[{'stage': 'research', 'plan': focused(), 'priority': 1, 'instructions': 'Inspect the polling source'}])
        result = await supervisor.dispatch(decision)
        assert len(result['scheduled']) == 1
        started = await supervisor.advance(steps=1)
        await asyncio.gather(supervisor.work[started['task_id']])
        assert store.db.execute('SELECT state FROM campaign_queue').fetchone()[0] == 'completed'
        assert store.db.execute('SELECT attempts FROM research_jobs').fetchone()[0] == 1
        assert store.db.execute("SELECT 1 FROM activity WHERE kind='scheduled_decision'").fetchone()
    finally:
        await supervisor.close()


async def test_crash_after_claim_preserves_assignment_without_requeue(system):
    from omnitrix.scheduling import enqueue_batch
    from omnitrix.campaign import Campaign

    store, goal, _ = system
    enqueue_batch(store, goal, [{'stage': 'research', 'plan': focused(), 'priority': 1}])
    claimed = Campaign(store, goal).claim_next()
    supervisor = await Supervisor(store, goal, runtime=LocalRuntime()).open()
    try:
        assert store.db.execute('SELECT state FROM campaign_queue').fetchone()[0] == 'interrupted'
        assert Campaign(store, goal).claim_next() is None
        assert supervisor.research.job(claimed['job_id'])['attempts'] == 0
    finally:
        await supervisor.close()


@pytest.mark.parametrize('report_saved', [False, True])
async def test_semantic_watchdog_ignores_fresh_reads_without_closed_obligations(system, report_saved):
    store, goal, _ = system
    started = asyncio.Event()
    now = time.time()

    class ResearchProgress:
        def progress_assessment(self, job_id):
            return {'job_id': job_id, 'legacy_scope_only': False, 'report_saved': report_saved, 'semantic_progress_at': now - 400,
                    'semantic_progress_count': 0, 'suggested_action': 'record_checkpoint',
                    'remaining_tool_calls': 2, 'unresolved_obligations': ['callee:helper.md']}

    supervisor = await Supervisor(store, goal, runtime=LocalRuntime(), research=ResearchProgress()).open()
    supervisor.housekeeper.cancel()
    await asyncio.gather(supervisor.housekeeper, return_exceptions=True)
    async def operation(task_id):
        started.set()
        await asyncio.Event().wait()
    try:
        task = await supervisor.submit('research', {'job_id': 'read-only-fixture'}, operation, seconds=1000)
        await started.wait()
        # A newly captured excerpt is activity, but it has not answered the
        # declared caller/callee obligation or closed a checkpoint.
        store.execute('UPDATE task_runtime SET last_progress=? WHERE task_id=?', (now, task))
        await supervisor.monitor(now=now, stall_seconds=300)
        expected_state = 'running' if report_saved else 'paused'
        assert store.db.execute('SELECT state FROM task_runtime WHERE task_id=?', (task,)).fetchone()[0] == expected_state
        expected_guidance = 0 if report_saved else 1
        assert store.db.execute("SELECT count(*) FROM activity WHERE kind='research_progress_guidance'").fetchone()[0] == expected_guidance
        await supervisor.monitor(now=now, stall_seconds=300)
        assert store.db.execute("SELECT count(*) FROM activity WHERE kind='research_progress_guidance'").fetchone()[0] == expected_guidance
    finally:
        await supervisor.close()
