"""Accelerated scheduler tests are not days of live model uptime."""
import json
from types import SimpleNamespace
import pytest
from omnitrix.cadence import Cadence
from omnitrix.campaign import Campaign
from omnitrix.store import Store
from omnitrix.spending import Spending
from test_campaign import setup


def test_three_virtual_days_preserve_wakes_deadline_spend_and_strategy_across_restarts(tmp_path):
    store, goal, campaign, _ = setup(tmp_path)
    cadence = Cadence(store, goal)
    cadence.configure(3600)
    store.execute("UPDATE campaigns SET state='running' WHERE goal_id=?", (goal,))
    saved = campaign.status()
    budget = Spending(store, goal)
    now = saved['created']
    decisions = 0
    while now < saved['deadline']:
        assert campaign.claim_decision(now=now)
        decisions += 1
        request = budget.reserve(100, 100, 'test')
        budget.settle(request, SimpleNamespace(input_tokens=100, output_tokens=10, input_tokens_details=None))
        state = cadence.defer('Simulated transient error; choose a new component', now=now)
        before = budget.status()['spent_nanos']
        assert not campaign.claim_decision(now=now+1)
        now = state['wake_at']
        store.db.close()
        store = Store(tmp_path/'state.db')
        campaign, cadence, budget = Campaign(store,goal), Cadence(store,goal), Spending(store,goal)
        assert campaign.status()['deadline'] == saved['deadline']
        assert cadence.status() == state
        assert budget.status()['spent_nanos'] == before
    assert not campaign.claim_decision(now=now)
    assert campaign.status()['state'] == 'deadline'
    assert campaign.status()['decisions_spent'] == decisions
    assert cadence.status()['strategy'] == decisions
    assert budget.status()['limit_usd'] == 25
    assert decisions > 3  # Repeated wake/review/reopen cycles, not just one terminal clock check.


def test_operator_stop_and_dollar_ceiling_are_not_restarted_by_cadence(tmp_path):
    store, goal, campaign, _ = setup(tmp_path)
    Cadence(store,goal).configure(1)
    campaign.stop('paused', 'Operator paused the campaign')
    assert not campaign.claim_decision()
    assert campaign.status()['state'] == 'paused'


async def test_continuous_completion_is_a_milestone_not_a_terminal_goal(tmp_path):
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan
    from omnitrix.campaign import CampaignSupervisor, ReadOnlyRuntime
    from omnitrix.models import Decision
    from omnitrix.frontier import Frontier
    from tests_helpers import FixtureAgents
    store, goal, campaign, _ = setup(tmp_path)
    Cadence(store,goal).configure(1)
    Frontier(store,goal).build()
    research = Research(store,FixtureAgents())
    job = research.plan(goal,'research',ResearchPlan(scope=['system.md'],question='Check the interval contract',
        hypothesis='Queue polling interval is 30 seconds',falsification='The source gives a different interval'))['job_id']
    assert (await research.attempt(job))['status'] == 'supported'
    supervisor = CampaignSupervisor(store,goal,runtime=ReadOnlyRuntime())
    decision = Decision(action='complete',rationale='A supported contract was established',instructions='Record this milestone',task_id=None,priority=1)
    result = await supervisor.dispatch(decision)
    assert result['milestone']['last_milestone_hypotheses'] == 1
    assert store.snapshot(goal)['goal']['status'] == 'active'
    with pytest.raises(ValueError,match='newly tested hypothesis'):
        await supervisor.dispatch(decision)
    assert store.db.execute("SELECT count(*) FROM activity WHERE kind='campaign_milestone'").fetchone()[0] == 1


def test_pacing_retains_budget_and_delays_model_work_instead_of_burning_it(tmp_path):
    store,goal,campaign,_=setup(tmp_path)
    cadence=Cadence(store,goal);cadence.configure(300)
    store.execute('UPDATE spending_limits SET spent_nanos=1000000000 WHERE goal_id=?',(goal,))
    now=campaign.status()['created']
    assert cadence.pace(now=now)
    wake=cadence.status()['wake_at']
    assert now < wake < campaign.status()['deadline']
    assert not campaign.claim_decision(now=now)
    assert Spending(store,goal).status()['spent_usd']==1


def test_watcher_restarts_sigkilled_owner_without_resetting_campaign(tmp_path):
    import subprocess,sys
    from pathlib import Path
    store,goal,campaign,_=setup(tmp_path)
    saved=campaign.status()
    code='''import os,signal,sys
from pathlib import Path
from omnitrix.store import Store
s=Store(sys.argv[1]);goal=sys.argv[2];marker=Path(sys.argv[3])
if not marker.exists():
 marker.write_text('crashed once')
 s.execute("UPDATE campaigns SET state='running',decisions_spent=3 WHERE goal_id=?",(goal,))
 os.kill(os.getpid(),signal.SIGKILL)
s.execute("UPDATE campaigns SET state='paused',reason='test complete' WHERE goal_id=?",(goal,))
'''
    child=[sys.executable,'-c',code,str(store.path),goal,str(tmp_path/'once')]
    from scripts.watch_campaign import watch
    result=watch(store.path,goal,command=child,restart_delay=.01)
    assert result['state']=='paused' and result['decisions_spent']==3
    assert result['deadline']==saved['deadline']
    assert store.db.execute("SELECT count(*) FROM activity WHERE kind='campaign_owner_restarted'").fetchone()[0]==1


async def test_live_scheduler_recovers_provider_error_and_continues_after_milestone(tmp_path):
    from openai import APIConnectionError
    from omnitrix.campaign import CampaignSupervisor, ReadOnlyRuntime
    from omnitrix.models import Decision
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan
    from tests_helpers import FixtureAgents
    store,goal,campaign,_=setup(tmp_path)
    store.execute('UPDATE campaigns SET max_decisions=3 WHERE goal_id=?',(goal,))
    Cadence(store,goal).configure(1)
    class Brain:
        calls=0
        async def decide(self,state):
            self.calls+=1
            if self.calls==1:
                raise APIConnectionError(request=SimpleNamespace(method='POST',url='https://example.invalid'))
            if self.calls==2:
                return Decision(action='research',rationale='Test a source contract',instructions='Read interval',task_id=None,priority=1,
                    research_plan=ResearchPlan(scope=['system.md'],question='Check the interval',hypothesis='Queue polling interval is 30 seconds',falsification='A different interval'))
            return Decision(action='complete',rationale='Supported source contract',instructions='Save milestone',task_id=None,priority=1)
    brain=Brain()
    supervisor=CampaignSupervisor(store,goal,brain=brain,runtime=ReadOnlyRuntime(),research=Research(store,FixtureAgents()))
    result=await campaign.drive(supervisor=supervisor,poll_seconds=.001)
    assert result['state']=='decision_budget' and result['decisions_spent']==3
    assert brain.calls==3
    assert Cadence(store,goal).status()['cycle']==2
    assert Cadence(store,goal).status()['failure_streak']==0
    assert store.snapshot(goal)['goal']['status']=='active'
    events=[r[0] for r in store.db.execute('SELECT kind FROM activity')]
    assert 'campaign_self_review' in events and 'campaign_milestone' in events


def test_watcher_default_cli_entrypoint_is_executable():
    import subprocess,sys
    result=subprocess.run([sys.executable,'-m','omnitrix','campaign','--help'],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert '--continuous' in result.stdout and '--prepare-only' in result.stdout
