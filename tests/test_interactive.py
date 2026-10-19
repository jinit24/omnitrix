import asyncio
import json
import sys
import pytest
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.supervisor import Supervisor


class Runtime:
    def __init__(self,lose_ack=False): self.sent=[];self.interrupted=[];self.lose_ack=lose_ack
    async def steer(self,thread,turn,text,message_id):
        self.sent.append((thread,turn,text,message_id))
        if self.lose_ack: raise RuntimeError('Acknowledgment lost after delivery')
        return {'turnId':turn}
    async def interrupt(self,thread,turn): self.interrupted.append((thread,turn))
    async def close(self): pass
    async def reconcile(self,thread): return {'thread':{'id':thread,'turns':[{'id':'turn','status':'completed','items':[]}]}}
    async def answer(self,request_id,answers): self.sent.append((request_id,answers))


@pytest.fixture
def state(tmp_path):
    store=Store(tmp_path/'state.db')
    goal=store.create_goal('Improve system',['Preserve correctness'],tmp_path)
    return store,goal


async def test_decision_budget_survives_checkpoint_and_cannot_refill_on_open(state):
    store, goal = state
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    supervisor.autonomy = 7
    supervisor.autonomy -= 1
    await supervisor.close(preserve_autonomy=True)
    store.db.close()
    fresh = Store(store.path)
    restored = await Supervisor(fresh, goal, runtime=Runtime()).open()
    assert restored.autonomy == restored.status()['remaining_decisions'] == 6
    await restored.close()
    again = await Supervisor(fresh, goal, runtime=Runtime()).open()
    assert again.autonomy == 0  # An intentional stop is not a restart authorization.
    await again.close()


async def long_worker(supervisor,started,release,ignore_cancel=False):
    async def operation(task_id):
        supervisor.store.execute('UPDATE task_runtime SET session_id=?,turn_id=? WHERE task_id=?',('thread','turn',task_id))
        started.set()
        try: await release.wait()
        except asyncio.CancelledError:
            if not ignore_cancel: raise
        return {'finding':'durable result'}
    return await supervisor.submit('mock',{'scope':'service'},operation)


async def test_responsive_progress_policy_delivery_and_restart(state):
    store,goal=state
    runtime=Runtime()
    supervisor=await Supervisor(store,goal,runtime=runtime).open()
    started,release=asyncio.Event(),asyncio.Event()
    task=await long_worker(supervisor,started,release)
    await started.wait()
    assert (await asyncio.wait_for(asyncio.to_thread(lambda: 'responsive'),.2))=='responsive'
    assert supervisor.status()['active'][0]['state']=='running'
    # Policy is an explicit proposal; it cannot mutate durable instructions silently.
    proposal=supervisor.control.propose(goal,'policy',{'instructions':'Be more thorough about correctness','affected_tasks':[task]})
    assert supervisor.control.current(goal)['policy']['version']==1
    await supervisor.accept(proposal)
    await asyncio.sleep(0)
    assert runtime.sent and 'correctness' in runtime.sent[0][2]
    assert supervisor.control.current(goal)['policy']['version']==2
    assert store.db.execute('SELECT count(*) FROM policy_versions').fetchone()[0]==2
    session=supervisor.control.session(goal)
    await session.add_items([{'role':'user','content':'Remember the correctness principle'}])
    session.close()
    mid=supervisor.control.message(goal,'steering',{'instruction':'Inspect another service'},task,'manager_to_worker')
    await supervisor.close()
    fresh=Store(store.path)
    restored=await Supervisor(fresh,goal,runtime=Runtime()).open()
    assert any(m['id']==mid for m in restored.control.pending(goal))
    session=restored.control.session(goal)
    assert any('correctness' in str(m) for m in await session.get_items())
    session.close()
    assert len(restored.status()['tasks'])==1
    await restored.close()


async def test_requirements_revision_invalidates_only_affected_work(state):
    store,goal=state
    supervisor=await Supervisor(store,goal,runtime=Runtime()).open()
    start1,start2,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    affected=await long_worker(supervisor,start1,release,ignore_cancel=True)
    unaffected=await long_worker(supervisor,start2,release)
    await start1.wait();await start2.wait()
    proposal=supervisor.control.propose(goal,'requirements',{'constraint':'Stop researching service A',
        'add_requirements':['Check service B'],'affected_tasks':[affected]})
    await supervisor.accept(proposal)
    await asyncio.sleep(.01)
    current=supervisor.control.current(goal)
    assert current['goal']['revision']==2
    assert current['goal']['requirements']==['Preserve correctness','Check service B']
    assert store.db.execute('SELECT state FROM task_runtime WHERE task_id=?',(unaffected,)).fetchone()[0]=='running'
    release.set();await asyncio.sleep(.01)
    assert store.db.execute("SELECT 1 FROM messages WHERE kind='stale_result' AND task_id=?",(affected,)).fetchone()
    assert store.task(unaffected)['status']=='completed'
    await supervisor.close()


async def test_lost_ack_is_reconciled_not_replayed(state):
    store,goal=state
    runtime=Runtime(lose_ack=True)
    supervisor=await Supervisor(store,goal,runtime=runtime).open()
    started,release=asyncio.Event(),asyncio.Event()
    task=await long_worker(supervisor,started,release)
    await started.wait()
    message=supervisor.control.message(goal,'steering',{'text':'Change approach'},task,'manager_to_worker',message_id='stable-id')
    assert supervisor.control.message(goal,'steering',{'text':'duplicate'},task,'manager_to_worker',message_id='stable-id')==message
    with pytest.raises(RuntimeError,match='Acknowledgment'):
        await supervisor.deliver(message)
    assert (await supervisor.deliver(message))['blocked']
    assert len(runtime.sent)==1
    assert store.db.execute('SELECT count(*) FROM messages WHERE id=?',(message,)).fetchone()[0]==1
    await supervisor.close()


async def test_waiting_worker_does_not_spend_manager_tokens(state):
    store,goal=state
    supervisor=await Supervisor(store,goal,runtime=Runtime()).open()
    started,release=asyncio.Event(),asyncio.Event()
    task=await long_worker(supervisor,started,release)
    await started.wait()
    await supervisor.on_runtime_event({'id':7,'method':'item/tool/requestUserInput','params':{
        'threadId':'thread','turnId':'turn','questions':[{'id':'q','question':'Which dependency?'}]}})
    await supervisor.monitor(now=10**12)
    assert store.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='waiting'
    assert not store.db.execute('SELECT 1 FROM decisions').fetchone()
    question=store.db.execute("SELECT id FROM messages WHERE kind='question'").fetchone()[0]
    await supervisor.answer(question,{'q':['database']})
    assert store.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='running'
    assert store.db.execute("SELECT state FROM messages WHERE kind='answer'").fetchone()[0]=='sent_no_ack'
    await supervisor.close()


async def test_stall_and_worker_death_are_uncertain_not_success(state):
    store,goal=state
    supervisor=await Supervisor(store,goal,runtime=Runtime()).open()
    proc=None
    async def operation(task_id):
        nonlocal proc
        proc=await asyncio.create_subprocess_exec(sys.executable,'-c','import time;time.sleep(60)')
        await proc.wait()
        if proc.returncode: raise RuntimeError('Killed worker; reconcile artifacts')
    task=await supervisor.submit('mock',{},operation)
    while proc is None: await asyncio.sleep(0)
    proc.kill()
    await asyncio.gather(supervisor.work[task],return_exceptions=True)
    assert store.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='uncertain'
    assert (await supervisor.reconcile(task))['blocked']
    await supervisor.close()


async def test_only_one_supervisor_can_own_database(state):
    store,goal=state
    one=await Supervisor(store,goal,runtime=Runtime()).open()
    with pytest.raises(RuntimeError,match='owns'):
        await Supervisor(Store(store.path),goal,runtime=Runtime()).open()
    await one.close()


async def test_other_process_can_send_status_without_becoming_manager(state):
    import subprocess
    store,goal=state
    supervisor=await Supervisor(store,goal,runtime=Runtime()).open()
    started,release=asyncio.Event(),asyncio.Event()
    task=await long_worker(supervisor,started,release)
    await started.wait()
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','omnitrix.cli','--db',str(store.path),
        'send',goal,'--command','status','--id','remote-status',stdout=asyncio.subprocess.PIPE)
    await process.communicate()
    for _ in range(20):
        row=store.db.execute("SELECT body FROM messages WHERE id='reply:remote-status'").fetchone()
        if row: break
        await asyncio.sleep(.05)
    assert row
    assert json.loads(row[0])['active'][0]['state']=='running'
    assert not release.is_set()
    await supervisor.close()


async def test_bad_model_scope_returns_targeted_feedback_instead_of_crashing(state):
    from omnitrix.models import Decision
    from omnitrix.research_models import ResearchPlan
    store,goal=state
    class BadBrain:
        async def decide(self,snapshot):
            return Decision(action='discover',rationale='Map the service',instructions='Inspect sources',task_id=None,priority=1,
                research_plan=ResearchPlan(scope=['.'],question='Where is the retry behavior?'))
    supervisor=await Supervisor(store,goal,brain=BadBrain(),runtime=Runtime()).open()
    result=await supervisor.advance(steps=5)
    assert result['rejected'] and not result['escalated']
    assert supervisor.control.pending(goal)[0]['kind']=='decision_rejected'
    await supervisor.advance();result=await supervisor.advance()
    assert result['escalated'] and supervisor.autonomy==0
    await supervisor.close()


async def test_implementation_goal_cannot_complete_without_any_implementation(state):
    from omnitrix.models import Decision
    store,goal=state
    Control(store).require_implementation(goal)
    class Brain:
        async def decide(self,snapshot):
            return Decision(action='complete',rationale='Suggestions are sufficient',instructions='Done',task_id=None,priority=1)
    supervisor=await Supervisor(store,goal,brain=Brain(),runtime=Runtime()).open()
    assert (await supervisor.advance(steps=2))['blocked']
    assert supervisor.status()['goal']['status']!='completed'
    assert supervisor.autonomy==1
    await supervisor.close()


async def test_manager_sdk_turn_exhaustion_pauses_without_crashing_or_retrying(state):
    from agents.exceptions import MaxTurnsExceeded
    store,goal=state
    class Brain:
        async def decide(self,snapshot):
            raise MaxTurnsExceeded('Max turns (8) exceeded')
    supervisor=await Supervisor(store,goal,brain=Brain(),runtime=Runtime()).open()
    result=await supervisor.advance(steps=5)
    assert result['blocked']['error']=='MaxTurnsExceeded'
    assert supervisor.status()['goal']['status']=='paused'
    assert supervisor.autonomy==0
    assert not supervisor.work
    await supervisor.close()


async def test_rejected_research_cannot_be_reported_as_missing_permission(state):
    from omnitrix.models import Decision
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan, ResearchBudget
    from tests_helpers import FixtureAgents
    store, goal = state
    workspace = store.snapshot(goal)['goal']['workspace']
    from pathlib import Path
    Path(workspace, 'system.md').write_text('Service queue uses polling interval=30 seconds\n')
    Control(store).require_implementation(goal)
    research = Research(store, FixtureAgents(misleading=True))
    spec = ResearchPlan(scope=['system.md'], question='What is the polling interval?',
        hypothesis='Queue polling interval is 30 seconds', falsification='The source states a different interval',
        budget=ResearchBudget(attempts=1))
    job = research.plan(goal, 'research', spec)['job_id']
    assert (await research.attempt(job))['status'] == 'escalated'
    class Brain:
        async def decide(self, snapshot):
            return Decision(action='pause', rationale='Missing permission', instructions='Authorize implementation',
                task_id=None, priority=1, pause_reason='authorization')
    supervisor = await Supervisor(store, goal, brain=Brain(), runtime=Runtime()).open()
    try:
        result = await supervisor.advance(steps=3)
        assert 'Research acceptance' in result['rejected']['error']
        assert supervisor.status()['goal']['status'] == 'active'
        pause = Decision(action='pause', rationale='Research rejected unsupported evidence',
            instructions='Inspect captured sources and replan within the remaining goal budget',
            task_id=None, priority=1, pause_reason='research')
        result = await supervisor.dispatch(pause)
        assert result['research_state']['latest_job']['id'] == job
        assert 'Unknown' in result['research_state']['latest_job']['feedback']
        assert not store.db.execute('SELECT 1 FROM implementation_authorizations').fetchone()
    finally:
        await supervisor.close()


@pytest.mark.parametrize('reason', ['external_input', 'research'])
async def test_recoverable_scope_error_does_not_become_user_scope_question(state, reason):
    from pathlib import Path
    from omnitrix.models import Decision
    from omnitrix.research_models import ResearchPlan
    store, goal = state
    Path(store.snapshot(goal)['goal']['workspace'], 'contract.md').write_text('A local contract is available')
    Control(store).require_implementation(goal)
    class Brain:
        async def decide(self, snapshot):
            return Decision(action='discover', rationale='Read contract and directory', instructions='Inspect sources',
                task_id=None, priority=1, research_plan=ResearchPlan(scope=['contract.md', '.'],
                    question='Which contract can establish the defect?'))
    supervisor = await Supervisor(store, goal, brain=Brain(), runtime=Runtime()).open()
    try:
        result = await supervisor.advance(steps=3)
        assert result['rejected']['observed_valid_scope'] == ['contract.md']
        pause = Decision(action='pause', rationale='Choose scope', instructions='Which files should I inspect?',
            task_id=None, priority=1, pause_reason=reason)
        with pytest.raises(ValueError, match='Correct the rejected plan'):
            await supervisor.dispatch(pause)
        assert supervisor.status()['goal']['status'] == 'active'
    finally:
        await supervisor.close()


async def test_malformed_decisions_get_a_bounded_correction_without_dispatch(state):
    from agents.exceptions import ModelBehaviorError
    store, goal = state
    class Brain:
        async def decide(self, snapshot):
            raise ModelBehaviorError('Invalid JSON')
    supervisor = await Supervisor(store, goal, brain=Brain(), runtime=Runtime()).open()
    try:
        first = await supervisor.advance(steps=10)
        assert first['rejected'] and not first['escalated']
        second = await supervisor.advance()
        assert second['escalated']
        assert supervisor.status()['goal']['status'] == 'paused'
        assert supervisor.autonomy == 0
        assert not store.db.execute('SELECT 1 FROM tasks').fetchone()
        assert not store.db.execute('SELECT 1 FROM decisions').fetchone()
    finally:
        await supervisor.close()


async def test_retryable_research_is_not_a_missing_manager_tool(state):
    from pathlib import Path
    from omnitrix.models import Decision
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan
    from tests_helpers import FixtureAgents
    store, goal = state
    Path(store.snapshot(goal)['goal']['workspace'], 'system.md').write_text('Queue polling interval=30 seconds\n')
    Control(store).require_implementation(goal)
    research = Research(store, FixtureAgents(misleading=True))
    job = research.plan(goal, 'research', ResearchPlan(scope=['system.md'], question='Check interval',
        hypothesis='Queue interval is 30 seconds', falsification='Source gives a different interval'))['job_id']
    assert (await research.attempt(job))['status'] == 'insufficient'
    supervisor = await Supervisor(store, goal, runtime=Runtime()).open()
    try:
        pause = Decision(action='pause', rationale='Manager has no source read tool',
            instructions='Wait for research', task_id=None, priority=1, pause_reason='research')
        with pytest.raises(ValueError, match='Research is still executable'):
            await supervisor.dispatch(pause)
        assert supervisor.status()['goal']['status'] == 'active'
        assert research.job(job)['attempts'] == 1
        pause.pause_reason = 'external_input'
        pause.instructions = 'Credentials for a required external service are unavailable'
        await supervisor.dispatch(pause)
        assert supervisor.status()['goal']['status'] == 'paused'
    finally:
        await supervisor.close()
