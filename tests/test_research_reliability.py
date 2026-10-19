"""Research must retain paid work and recover without inventing external sessions."""
import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnitrix.control import Control
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'source.md').write_text('Polling interval=30 seconds\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Research queue reliability', ['Preserve contracts'], tmp_path)
    spec = ResearchPlan(scope=['source.md'], question='What is the polling contract?',
        hypothesis='Polling interval is 30 seconds', falsification='Source specifies another interval')
    return store, goal, spec


def test_duplicate_research_is_scoped_to_goal(state):
    store, goal, spec = state
    other = store.create_goal('Audit another decision', ['Measure latency'],
        store.snapshot(goal)['goal']['workspace'])
    research = Research(store, FixtureAgents())
    first = research.plan(goal, 'research', spec)
    assert research.plan(goal, 'research', spec)['job_id'] == first['job_id']
    second = research.plan(other, 'research', spec)
    assert not second['duplicate']
    assert research.job(second['job_id'])['goal_id'] == other


@pytest.mark.parametrize('failure', ['exception', 'budget', 'cancellation'])
async def test_completed_report_survives_evaluator_failure(state, failure):
    store, goal, spec = state

    class FailsReview(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'evaluation':
                if failure == 'cancellation':
                    raise asyncio.CancelledError()
                raise RuntimeError('Evaluator unavailable')
            return await super().run(role, context, tools)

    research = Research(store, FailsReview())
    if failure == 'budget':
        research.configure(goal, 1)
    job = research.plan(goal, 'research', spec)['job_id']
    if failure == 'cancellation':
        with pytest.raises(asyncio.CancelledError):
            await research.attempt(job)
    else:
        outcome = await research.attempt(job)
        assert outcome['report']['findings'][0]['classification'] == 'claim'
    reopened = Store(store.path)
    row = reopened.db.execute('SELECT * FROM research_runs WHERE job_id=?', (job,)).fetchone()
    assert json.loads(row['report'])['supporting_evidence']
    assert row['evaluation'] is None
    assert row['finished']
    assert not reopened.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()


async def test_stale_report_is_retained_with_original_input(state):
    store, goal, spec = state
    control = Control(store)

    class ChangesContext(FixtureAgents):
        async def run(self, role, context, tools):
            report = await super().run(role, context, tools)
            proposal = control.propose(goal, 'research_context',
                {'brief': 'Require production observations', 'rubric': ['Measure latency']})
            control.accept(goal, proposal)
            return report

    research = Research(store, ChangesContext())
    job = research.plan(goal, 'research', spec)['job_id']
    result = await research.attempt(job)
    assert result['status'] == 'stale'
    assert result['report']['findings']
    context = store.db.execute('SELECT * FROM research_run_contexts WHERE run_id=?', (result['run_id'],)).fetchone()
    assert json.loads(context['context'])['research_context']['version'] == 0
    assert json.loads(context['revisions'])['goal']['revision'] == 1
    assert control.research_context(goal)['version'] == 1


async def test_each_role_input_is_durable_and_report_precedes_review(state):
    store, goal, spec = state

    class InspectsLedger(FixtureAgents):
        async def run(self, role, context, tools):
            row = store.db.execute('SELECT context,revisions FROM research_run_contexts WHERE run_id=? AND role=?',
                (tools.run_id, role)).fetchone()
            assert json.loads(row['context']) == context
            assert json.loads(row['revisions'])['goal']['revision'] == 1
            if role == 'evaluation':
                saved = store.db.execute('SELECT report FROM research_runs WHERE id=?', (tools.run_id,)).fetchone()[0]
                assert json.loads(saved) == context['report']
                assert context['evidence'][0]['excerpt']
            return await super().run(role, context, tools)

    research = Research(store, InspectsLedger())
    job = research.plan(goal, 'research', spec)['job_id']
    result = await research.attempt(job)
    assert result['status'] == 'supported'
    reopened = Store(store.path)
    assert {r[0] for r in reopened.db.execute('SELECT role FROM research_run_contexts')} == {'research', 'evaluation'}


async def test_sigkill_during_review_can_reconcile_local_research(state, tmp_path):
    store, goal, _ = state
    marker = tmp_path / 'review-started'
    code = '''
import asyncio,sys
from pathlib import Path
from omnitrix.store import Store
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.models import Decision
from omnitrix.supervisor import Supervisor
from tests_helpers import FixtureAgents
class Runtime:
 async def close(self): pass
class PausesReview(FixtureAgents):
 async def run(self,role,context,tools):
  if role=='evaluation':
   Path(sys.argv[3]).write_text('ready')
   await asyncio.Event().wait()
  return await super().run(role,context,tools)
async def main():
 s=Store(sys.argv[1]);g=sys.argv[2]
 sup=await Supervisor(s,g,runtime=Runtime(),research=Research(s,PausesReview())).open()
 spec=ResearchPlan(scope=['source.md'],question='What is the polling contract?',hypothesis='Polling interval is 30 seconds',falsification='Source specifies another interval')
 await sup.dispatch(Decision(action='research',rationale='Inspect contract',instructions='Read contract',task_id=None,priority=1,research_plan=spec))
 await asyncio.Event().wait()
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(store.path), goal, str(marker)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert marker.exists(), process.communicate(timeout=1)
        process.kill()
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    class Runtime:
        async def reconcile(self, thread):
            raise AssertionError('SDK research has no external thread')

        async def close(self):
            pass

    class Brain:
        async def decide(self, snapshot):
            return Decision(action='pause', rationale='Review retained evidence',
                instructions='Review findings', task_id=None, priority=1)

    fresh = Store(store.path)
    supervisor = await Supervisor(fresh, goal, runtime=Runtime(), brain=Brain()).open()
    try:
        task = fresh.db.execute('SELECT task_id FROM task_runtime').fetchone()[0]
        assert fresh.db.execute('SELECT state FROM task_runtime').fetchone()[0] == 'reconciled'
        assert fresh.db.execute('SELECT attempts FROM research_jobs').fetchone()[0] == 1
        assert not (await supervisor.advance(1)).get('blocked')
        result = await supervisor.reconcile(task)
        assert result['status'] == 'interrupted'
        assert result['report']['findings']
        assert result['evaluation'] is None
        assert result['evidence'] and not result['evidence'][0]['stale']
        assert fresh.db.execute('SELECT finished FROM research_runs').fetchone()[0]
        assert fresh.db.execute('SELECT state FROM task_runtime').fetchone()[0] == 'reconciled'
        assert any(m['kind']=='reconciled_outcome' for m in fresh.snapshot(goal)['worker_messages'])
        assert not (await supervisor.advance(1)).get('blocked')
        assert fresh.db.execute('SELECT attempts FROM research_jobs').fetchone()[0] == 1
        job = fresh.db.execute('SELECT id FROM research_jobs').fetchone()[0]
        with pytest.raises(ValueError, match='targeted feedback'):
            await supervisor.research.attempt(job)
    finally:
        await supervisor.close()


async def test_new_research_receives_manager_handoff(tmp_path):
    from omnitrix.store import Store
    from omnitrix.supervisor import Supervisor
    from omnitrix.models import Decision
    from omnitrix.research_models import ResearchPlan
    (tmp_path/'source.md').write_text('contract')
    store=Store(tmp_path/'state.db')
    goal=store.create_goal('Investigate contracts',['Cite facts'],tmp_path)
    class ResearchDouble:
        def job(self,job_id): return {'spec': '{"budget":{"seconds":10}}'}
        def plan(self,*args): return {'duplicate':False,'job_id':'new-job'}
        async def attempt(self,job_id,feedback=''):
            assert feedback=='Inspect the failure path and contrary tests before concluding.'
            return {'status':'discovered'}
    supervisor=await Supervisor(store,goal,research=ResearchDouble()).open()
    try:
        await supervisor.dispatch(Decision(action='discover',rationale='Find valuable uncertainty',instructions='Inspect the failure path and contrary tests before concluding.',task_id=None,priority=1,research_plan=ResearchPlan(scope=['source.md'],question='What contract matters most?')))
        import asyncio
        await asyncio.gather(*list(supervisor.work.values()))
        assert store.db.execute('SELECT status FROM tasks').fetchone()[0]=='completed'
    finally:
        await supervisor.close()


def test_focused_scope_limit_is_visible_in_structured_output_schema():
    from omnitrix.models import Decision
    schema=Decision.model_json_schema()
    assert schema['$defs']['FocusedResearchPlan']['properties']['scope']['maxItems']==8
    assert schema['$defs']['DiscoveryResearchPlan']['properties']['scope']['maxItems']==20
    from omnitrix.research_models import ResearchPlan
    plan=ResearchPlan(scope=['a.md'],question='Which question matters?',hypothesis='A behavior holds',falsification='The opposite is observed')
    decision=Decision(action='research',rationale='Bounded investigation',instructions='Inspect',task_id=None,priority=1,research_plan=plan)
    assert decision.research_plan.scope==['a.md']
