import json
import subprocess
import sys
from pathlib import Path
import pytest
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.implementation import Implementations, ImplementationSpec, AcceptanceCheck, git
from tests_helpers import FixtureAgents


class CodingRuntime:
    def __init__(self,fail_first=False,always_fail=False):
        self.calls=0;self.fail_first=fail_first;self.always_fail=always_fail;self.prompts=[]
    async def start(self,workspace,instructions,**kwargs):
        self.calls+=1;self.prompts.append(json.loads(instructions))
        bad=self.always_fail or self.fail_first and self.calls==1
        (Path(workspace)/'service.py').write_text('def double(value):\n    return value * '+('3' if bad else '2')+'\n')
        return 'thread',f'turn-{self.calls}'
    async def wait(self,*args): return {'status':'completed','items':[]}


@pytest.fixture
async def project(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init','-b','main',str(repo)],check=True,capture_output=True)
    (repo/'service.py').write_text('def double(value):\n    return value\n')
    (repo/'system.md').write_text('Service queue uses polling interval=30 seconds\n')
    git(repo,'add','.')
    git(repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','Fixture')
    store=Store(tmp_path/'state'/'state.db')
    goal=store.create_goal('Correct double()',['Double positive numbers','Handle zero'],repo)
    research=Research(store,FixtureAgents())
    plan=ResearchPlan(scope=['system.md'],question='Which correctness issue to fix?',
        hypothesis='Queue polling interval is 30 seconds',falsification='Source states another interval')
    job=research.plan(goal,'research',plan)['job_id']
    assert (await research.attempt(job))['status']=='supported'
    return store,goal,repo,job


def spec(job,assumption=False):
    return ImplementationSpec(objective='Correct double() while preserving zero behavior',research_job=job,
        allowed_paths=['service.py'],checks=[
            AcceptanceCheck(requirement='Double positive numbers',argv=[sys.executable,'-c','from service import double; assert double(2)==4'],
                            failure_kind='assumption' if assumption else 'local'),
            AcceptanceCheck(requirement='Handle zero',argv=[sys.executable,'-c','from service import double; assert double(0)==0'])])


async def create(project,runtime,specification=None):
    store,goal,repo,job=project
    service=Implementations(store,runtime)
    authorization=service.authorize(goal,specification or spec(job),explicit=True)
    return service,service.create(authorization)


async def test_explicit_authorization_required(project):
    store,goal,_,job=project
    service=Implementations(store,CodingRuntime())
    with pytest.raises(ValueError,match='explicit'):
        service.authorize(goal,spec(job))
    incomplete=spec(job);incomplete.checks=incomplete.checks[:1]
    with pytest.raises(ValueError,match='Every mandatory'):
        service.authorize(goal,incomplete,explicit=True)


async def test_independent_test_failure_repairs_then_exact_revision_audit(project):
    runtime=CodingRuntime(fail_first=True)
    service,implementation=await create(project,runtime)
    original=git(project[2],'rev-parse','HEAD')
    result=await service.run(implementation)
    assert result['status']=='ready_for_audit',result
    assert runtime.calls==2
    assert runtime.prompts[1]['feedback']
    assert project[2].joinpath('service.py').read_text().endswith('return value\n')
    assert git(project[2],'rev-parse','HEAD')==original
    results=[dict(r) for r in project[0].db.execute('SELECT * FROM verifications')]
    assert any(not r['passed'] for r in results)
    assert len({r['candidate'] for r in results})==2
    assert service.audit(implementation)['passed']
    assert service.get(implementation)['status']=='completed'
    snapshot = project[0].snapshot(project[1])
    assert snapshot['goal']['status'] == 'completed'
    assert snapshot['latest_audit']['passed']
    assert {r['requirement'] for r in snapshot['verification_results'] if r['passed']} == {
        'Double positive numbers', 'Handle zero'}


async def test_incorrect_assumption_returns_for_additional_research(project):
    service,implementation=await create(project,CodingRuntime(always_fail=True),spec(project[3],assumption=True))
    assert (await service.run(implementation))['status']=='needs_research'
    # A manager can now select a different, bounded uncertainty instead of retrying code.
    research=Research(project[0],FixtureAgents())
    followup=ResearchPlan(scope=['system.md'],question='Does the service contract need revisiting?',
        hypothesis='Queue dependency behavior explains the failed assumption',falsification='Registry excludes dependency behavior')
    job=research.plan(project[1],'research',followup)['job_id']
    assert (await research.attempt(job))['status']=='supported'
    assert service.get(implementation)['attempts']==1


async def test_repeated_local_failure_exhausts_budget(project):
    runtime=CodingRuntime(always_fail=True)
    service,implementation=await create(project,runtime)
    assert (await service.run(implementation))['status']=='escalated'
    await service.run(implementation)
    assert runtime.calls==2


async def test_passing_tests_cannot_omit_explicit_requirement(project):
    service,implementation=await create(project,CodingRuntime())
    result=await service.run(implementation)
    assert result['status']=='ready_for_audit'
    project[0].execute("DELETE FROM verifications WHERE requirement='Handle zero'")
    assert not service.audit(implementation)['passed']
    assert project[0].snapshot(project[1])['goal']['status'] != 'completed'


async def test_requirement_change_revokes_candidate_approval(project):
    service,implementation=await create(project,CodingRuntime())
    await service.run(implementation)
    control=Control(project[0])
    proposal=control.propose(project[1],'requirements',{'constraint':'Also handle negatives','add_requirements':['Handle negatives']})
    control.accept(project[1],proposal)
    with pytest.raises(ValueError,match='revoked'):
        service.audit(implementation)


async def test_lost_worktree_ack_reconciles_existing_checkout(project):
    service,implementation=await create(project,CodingRuntime())
    row=service.get(implementation)
    first=service.prepare(row)
    project[0].execute("UPDATE external_actions SET state='pending',result=NULL")
    second=service.prepare(row)
    assert first==second
    assert project[0].db.execute('SELECT state FROM external_actions').fetchone()[0]=='confirmed'


async def test_completed_worker_after_lost_response_is_verified_without_recoding(project):
    class LostResponse(CodingRuntime):
        async def wait(self,*args): raise RuntimeError('Connection lost after code was written')
        async def reconcile(self,thread):
            return {'thread':{'id':thread,'turns':[{'id':'turn-1','status':'completed','items':[]}]}}
    runtime=LostResponse()
    service,implementation=await create(project,runtime)
    assert (await service.run(implementation))['status']=='uncertain'
    assert runtime.calls==1
    result=await service.reconcile(implementation)
    assert result['status']=='ready_for_audit'
    assert runtime.calls==1
    assert service.audit(implementation)['passed']


async def test_changed_independent_oracle_revokes_authorization(project,tmp_path):
    checker=tmp_path/'trusted.py';checker.write_text('assert True\n')
    specification=spec(project[3])
    specification.checks[0].argv=[sys.executable,str(checker)]
    service,implementation=await create(project,CodingRuntime(),specification)
    checker.write_text('assert False\n')
    with pytest.raises(ValueError,match='oracle changed'):
        service.auth(service.get(implementation)['authorization'])


async def test_manager_can_see_authorized_checks_instead_of_guessing_they_are_missing(project):
    service,implementation=await create(project,CodingRuntime())
    visible=project[0].snapshot(project[1])['implementations'][0]
    assert visible['id']==implementation
    assert visible['spec']['checks'][0]['argv']
    assert {c['requirement'] for c in visible['spec']['checks']}=={'Double positive numbers','Handle zero'}
    assert visible['authorization_revoked']==0
