import json
import pytest
from omnitrix.control import Control
from omnitrix.store import Store
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan, RubricAssessment
from tests_helpers import FixtureAgents


def setup(tmp_path):
    (tmp_path/'source.md').write_text('Polling interval=30 seconds')
    store=Store(tmp_path/'state.db')
    goal=store.create_goal('Improve reliability',['Preserve contracts'],tmp_path)
    control=Control(store)
    control.initialize(goal)
    proposal=control.propose(goal,'research_context',{'brief':'Focus on queue latency; production benefit is unmeasured.', 'rubric':['Cite the polling contract']})
    control.accept(goal,proposal)
    spec=ResearchPlan(scope=['source.md'],question='What is the polling interval?', hypothesis='Polling is 30 seconds',falsification='A different interval is specified',decision='Whether to change polling',expected_value='Reduce queue delay',stop_condition='Stop after establishing the contract')
    return store,goal,control,spec


async def test_context_reaches_roles_and_survives_restart(tmp_path):
    store,goal,control,spec=setup(tmp_path)
    class Agents(FixtureAgents):
        async def run(self,role,context,tools):
            assert context['research_context']==control.research_context(goal)
            assert context['goal']['requirements']==['Preserve contracts']
            assert 'policy' in context
            result=await super().run(role,context,tools)
            if role=='evaluation':
                result.rubric_assessments=[RubricAssessment(criterion='Cite the polling contract',verdict='met',reason='Source states interval',evidence_ids=result.evidence_ids)]
            return result
    service=Research(store,Agents())
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='supported'
    reopened=Store(store.path)
    assert Control(reopened).research_context(goal)['version']==1
    assert reopened.snapshot(goal)['research_context']['brief'].startswith('Focus')
    assert reopened.snapshot(goal)['research_agenda'][0]['status']=='supported'


async def test_missing_rubric_cannot_pass(tmp_path):
    store,goal,_,spec=setup(tmp_path)
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'research',spec)['job_id']
    result=await service.attempt(job)
    assert result['status']=='insufficient'
    assert 'every exact rubric' in result['feedback']
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()


async def test_context_change_during_research_fences_result(tmp_path):
    store,goal,control,spec=setup(tmp_path)
    class Changed(FixtureAgents):
        async def run(self,role,context,tools):
            result=await super().run(role,context,tools)
            proposal=control.propose(goal,'research_context',{'brief':'New direction','rubric':['Measure latency']})
            control.accept(goal,proposal)
            return result
    service=Research(store,Changed())
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='stale'
    assert not service.plan(goal,'research',spec)['duplicate']
    assert len(list(store.db.execute('SELECT * FROM research_context_versions')))==2


def test_planning_contract_and_checkpoint_budget(tmp_path):
    store,goal,_,spec=setup(tmp_path)
    service=Research(store,FixtureAgents())
    with pytest.raises(ValueError,match='expected_value'):
        service.plan(goal,'research',spec.model_copy(update={'decision':''}))


@pytest.mark.parametrize('verdict,ids', [('unmet',['source']),('met',[]),('met',['invented'])])
def test_rubric_cannot_be_bypassed(tmp_path,verdict,ids):
    from omnitrix.research_models import ResearchReport,Finding,Evaluation
    report=ResearchReport(findings=[Finding(statement='Fact',classification='claim',evidence_ids=['source'])],supporting_evidence=['source'],contradictory_evidence=[],uncertainties=[],recommendation='Review',blocked=False)
    evaluation=Evaluation(verdict='supported',evidence_ids=['source'],reason='Supported',feedback='Continue',rubric_assessments=[RubricAssessment(criterion='Contract',verdict=verdict,reason='Reviewed',evidence_ids=ids)])
    assert Research.gate(report,evaluation,[{'id':'source','stale':False}],['Contract']).verdict=='insufficient'


async def test_checkpoint_requires_evidence_and_does_not_extend_budget(tmp_path):
    store,goal,control,spec=setup(tmp_path)
    from omnitrix.knowledge import ResearchTools
    class Agents(FixtureAgents):
        async def run(self,role,context,tools):
            if role=='research':
                source=tools.read('source.md')
                before=tools.remaining
                tools.checkpoint([source['id']],'Production latency unknown','Inspect exported measurement')
                assert tools.remaining==before-1
                with pytest.raises(ValueError,match='current captured evidence'):
                    tools.checkpoint(['invented'],'Unknown','Read source')
            return await super().run(role,context,tools)
    service=Research(store,Agents())
    job=service.plan(goal,'research',spec)['job_id']
    await service.attempt(job)
    checkpoint=store.snapshot(goal)['research_checkpoints'][0]
    assert json.loads(checkpoint['detail'])['decision']=='Whether to change polling'


async def test_new_context_revokes_prior_research_acceptance(tmp_path):
    store,goal,control,spec=setup(tmp_path)
    class Reviewed(FixtureAgents):
        async def run(self,role,context,tools):
            result=await super().run(role,context,tools)
            if role=='evaluation':
                result.rubric_assessments=[RubricAssessment(criterion='Cite the polling contract',verdict='met',reason='Observed',evidence_ids=result.evidence_ids)]
            return result
    service=Research(store,Reviewed())
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='supported'
    proposal=control.propose(goal,'research_context',{'brief':'Require production evidence','rubric':['Measure production latency']})
    control.accept(goal,proposal)
    assert service.job(job)['status']=='stale'
    assert not store.db.execute("SELECT 1 FROM knowledge WHERE classification='verified'").fetchone()
