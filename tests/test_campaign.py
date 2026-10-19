"""Queue durability, fair scheduling and stale-work fences without model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest

from omnitrix.audit import Audit
from omnitrix.campaign import Campaign, AGE_SECONDS
from omnitrix.control import Control
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store


@pytest.fixture
def state(tmp_path):
    for area in ('pkg','docs'):
        path = tmp_path / 'sources' / area
        path.mkdir(parents=True)
        (path/'one.txt').write_text('Bounded contract with tests and contrary evidence\n')
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Retain an audit program', ['No invented work'], tmp_path)
    Control(store).initialize(goal)
    audit = Audit(store,goal)
    audit.initialize([{'path':area,'files':1,'bytes':50,'kind':'repository area'} for area in ('pkg','docs')])
    return store,goal,tmp_path


def plan(store,goal,area,number=0):
    spec = ResearchPlan(primary_area=area, scope=[f'sources/{area}/one.txt'],
        question=f'Which bounded contract should be investigated for case {number}?',
        hypothesis=f'Contract {number} has a measurable property', falsification='Current source contradicts the property')
    return Research(store).plan(goal,'research',spec)['job_id']


def test_queue_survives_restart_and_never_invents_or_replays_work(state):
    store,goal,_ = state
    campaign = Campaign(store,goal)
    job = plan(store,goal,'pkg')
    queued = campaign.enqueue(job,2,'Inspect both support and counterexample')
    assert campaign.enqueue(job,2,'Inspect both support and counterexample')['id'] == queued['id']
    with pytest.raises(ValueError,match='immutable'):
        campaign.enqueue(job,1,'Changed assignment')
    store.db.close()
    store = Store(store.path)
    campaign = Campaign(store,goal)
    claimed = campaign.claim_next()
    assert claimed['job_id'] == job and claimed['instructions'].startswith('Inspect')
    assert claimed['claim_token']
    assert campaign.claim_next() is None
    store.db.close()
    fresh = Store(store.path)
    assert Campaign(fresh,goal).claim_next() is None
    assert Campaign(fresh,goal).summary()['queue']['claimed'] == 1
    assert fresh.db.execute('SELECT count(*) FROM research_jobs').fetchone()[0] == 1
    fresh.db.close()


def test_fairness_prioritizes_neglected_areas_and_ages_waiting_work(state,monkeypatch):
    store,goal,_ = state
    clock = [1000.]
    monkeypatch.setattr('omnitrix.campaign.time.time',lambda:clock[0])
    campaign = Campaign(store,goal)
    first, second, neglected = plan(store,goal,'pkg',1), plan(store,goal,'pkg',2), plan(store,goal,'docs',3)
    campaign.enqueue(first,1);clock[0] += 1;campaign.enqueue(second,1)
    assert campaign.claim_next()['job_id'] == first
    campaign.complete_job(first,{'status':'insufficient'})
    clock[0] += 1;campaign.enqueue(neglected,1)
    assert campaign.claim_next()['job_id'] == neglected
    campaign.complete_job(neglected,{'status':'falsified'})
    assert campaign.claim_next()['job_id'] == second
    campaign.complete_job(second,{'status':'supported'})
    low = plan(store,goal,'docs',4);campaign.enqueue(low,5)
    clock[0] += 5*AGE_SECONDS
    high = plan(store,goal,'pkg',5);campaign.enqueue(high,1)
    assert campaign.claim_next()['job_id'] == low


@pytest.mark.parametrize('kind,body',[
    ('policy',{'instructions':'Require changed policy'}),
    ('requirements',{'constraint':'Observe actual caller contracts','add_requirements':['Keep contrary evidence']}),
    ('research_context',{'brief':'New evaluation contract','rubric':['Inspect counterexamples']})])
def test_ready_assignments_are_fenced_after_revision_changes(state,kind,body):
    store,goal,_ = state
    campaign = Campaign(store,goal);job=plan(store,goal,'pkg')
    campaign.enqueue(job)
    control=Control(store);control.accept(goal,control.propose(goal,kind,body))
    assert campaign.claim_next() is None
    assert campaign.summary()['queue']['invalidated'] == 1
    assert 'changed' in store.db.execute('SELECT reason FROM campaign_queue').fetchone()[0]


def test_source_and_contract_changes_invalidate_ready_work(state):
    store,goal,path=state
    campaign=Campaign(store,goal);job=plan(store,goal,'pkg')
    campaign.enqueue(job)
    (path/'sources/pkg/one.txt').write_text('Changed source contract')
    assert campaign.claim_next() is None
    assert 'source revision changed' in store.db.execute('SELECT reason FROM campaign_queue').fetchone()[0]
    other=plan(store,goal,'docs');campaign.enqueue(other)
    spec=json.loads(store.db.execute('SELECT spec FROM research_jobs WHERE id=?',(other,)).fetchone()[0])
    spec['question']='A silently substituted research question'
    store.execute('UPDATE research_jobs SET spec=? WHERE id=?',(json.dumps(spec),other))
    assert campaign.claim_next() is None
    assert campaign.summary()['queue']['invalidated'] == 2


def test_claim_is_atomic_across_independent_connections(state):
    store,goal,_=state
    campaign=Campaign(store,goal);job=plan(store,goal,'pkg');campaign.enqueue(job)
    def claim():
        local=Store(store.path)
        try:return Campaign(local,goal).claim_next()
        finally:local.db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:claim(),range(2)))
    assert sum(result is not None for result in results)==1
    assert store.db.execute("SELECT count(*) FROM campaign_events WHERE kind='claimed'").fetchone()[0]==1


def test_batch_rollback_and_claim_boundary_are_durable(state):
    store,goal,_=state
    campaign=Campaign(store,goal);job=plan(store,goal,'pkg')
    store.db.execute('BEGIN IMMEDIATE')
    campaign.enqueue(job)
    assert store.db.in_transaction
    with pytest.raises(RuntimeError,match='committed'):
        campaign.claim_next()
    store.db.rollback()
    assert campaign.summary()['queued_jobs']==0
    campaign.enqueue(job)
    campaign.claim_next()
    result=campaign.complete_job(job,{'status':'uncertain','error':'Process outcome unknown'})
    assert result['state']=='uncertain'
    assert campaign.complete_job(job,{'status':'uncertain','error':'Process outcome unknown'})['id']==result['id']
    with pytest.raises(ValueError,match='immutable'):
        campaign.complete_job(job,{'status':'supported'})
    summary=campaign.summary()
    assert summary['run_status']=='blocked'
    assert summary['program_status']=='incomplete'
    assert summary['whole_audit_complete'] is False
    assert not campaign.completion_status()['complete_coverage_floor_met']


def test_unplanned_or_foreign_jobs_and_missing_primary_area_are_rejected(state):
    store,goal,path=state
    campaign=Campaign(store,goal)
    with pytest.raises(ValueError,match='already be planned'):
        campaign.enqueue('invented')
    other=store.create_goal('Other program',[],path)
    Audit(store,other).initialize([{'path':'pkg','files':1,'bytes':50,'kind':'repository area'}])
    foreign=plan(store,other,'pkg')
    with pytest.raises(ValueError,match='already be planned'):
        campaign.enqueue(foreign)
    spec=ResearchPlan(scope=['sources/pkg/one.txt'],question='Which contract should be evaluated?',hypothesis='The source states an invariant',falsification='Source contradicts the invariant')
    job=Research(store).plan(goal,'research',spec)['job_id']
    with pytest.raises(ValueError,match='primary_area'):
        campaign.enqueue(job)


def test_area_priority_and_paused_budgeted_run_remain_distinct_from_program(state):
    store,goal,_=state
    audit=Audit(store,goal)
    audit.update('pkg','triaged',1,'Runtime contract has higher expected impact','Inspect the caller boundary')
    campaign=Campaign(store,goal)
    older=plan(store,goal,'docs',1);newer=plan(store,goal,'pkg',2)
    campaign.enqueue(older,3);campaign.enqueue(newer,3)
    assert campaign.claim_next()['job_id']==newer
    campaign.complete_job(newer,{'status':'supported'})
    store.execute("UPDATE goals SET status='paused' WHERE id=?",(goal,))
    summary=campaign.summary()
    assert summary['queue']['ready']==1
    assert summary['run_status']=='paused'
    assert summary['budgeted_deliverable_status']=='paused'
    assert summary['program_status']=='incomplete'
    assert summary['whole_audit_complete'] is False
