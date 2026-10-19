import json

import pytest
from agents.tool_context import ToolContext

from omnitrix.campaign import Campaign
from omnitrix.investigator import Investigation,InvestigatorBrain,PreparedResearchAgents
from omnitrix.investigator import GoalReview,CriterionReview
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan,ResearchReport,Finding
from tests.test_campaign import setup
from tests_helpers import FixtureAgents


@pytest.mark.asyncio
async def test_investigator_handoff_retains_actual_sources_and_independent_review(tmp_path,monkeypatch):
    store,goal,campaign,workspace=setup(tmp_path)
    campaign.configure(strategy='investigator')
    store.execute("INSERT INTO knowledge VALUES('prior',?,?,?,'finding','Previous queue hypothesis','{}','rejected','[]',0)",
        (str(workspace.resolve()),goal,Research(store).plan(goal,'discovery',ResearchPlan(scope=['system.md'],question='What prior knowledge is available?'))['job_id']))
    async def run(agent,prompt,**kwargs):
        assert json.loads(prompt)['prior_knowledge_index'][0]['classification']=='rejected'
        assert any(tool.name=='search_knowledge' for tool in agent.tools)
        tool=next(t for t in agent.tools if t.name=='read_source')
        payload=json.dumps({'locator':'system.md','start_line':1,'line_count':80})
        context=ToolContext(context=None,tool_name=tool.name,tool_call_id='read',tool_arguments=payload)
        observed=json.loads(await tool.on_invoke_tool(context,payload))
        source=observed['id']
        return type('Result',(),{'final_output':Investigation(
            plan=ResearchPlan(scope=['system.md'],question='What is the queue polling interval?',hypothesis='The polling interval is 30 seconds',falsification='The registry gives another interval',opportunity_id='invented-model-id'),
            report=ResearchReport(findings=[Finding(statement='The polling interval is 30 seconds',classification='claim',evidence_ids=[source])],
                supporting_evidence=[source],contradictory_evidence=[],uncertainties=[],recommendation='Verify measured latency',blocked=False))})()
    monkeypatch.setattr('omnitrix.investigator.Runner.run',run)
    brain=InvestigatorBrain(store);decision=await brain.decide(store.snapshot(goal))
    assert decision.action=='research'
    assert decision.research_plan.opportunity_id is None
    prepared=PreparedResearchAgents(brain);prepared.reviewer=FixtureAgents()
    service=Research(store,prepared);job=service.plan(goal,'research',decision.research_plan)['job_id']
    result=await service.attempt(job)
    assert result['status']=='supported'
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==3
    assert store.db.execute("SELECT count(*) FROM research_jobs WHERE stage='investigation' AND status='investigated'").fetchone()[0]==1
    if hasattr(store,'_campaign_client'):await store._campaign_client.close()


def test_strategy_switch_never_resets_limits_or_spending(tmp_path):
    store,goal,campaign,_=setup(tmp_path)
    before=campaign.status()
    campaign.configure(strategy='investigator')
    after=campaign.status()
    assert after['strategy']=='investigator'
    for key in ('deadline','max_decisions','decisions_spent','spending'):assert before[key]==after[key]


@pytest.mark.asyncio
async def test_completion_audit_rejects_invented_findings_and_does_not_repeat_unchanged_review(tmp_path,monkeypatch):
    store,goal,campaign,_=setup(tmp_path)
    service=Research(store,FixtureAgents())
    job=service.plan(goal,'research',ResearchPlan(scope=['system.md'],question='What is the polling interval?',
        hypothesis='The interval is 30 seconds',falsification='A different interval appears'))['job_id']
    await service.attempt(job)
    calls=[]
    async def run(agent,prompt,**kwargs):
        calls.append(prompt)
        return type('Result',(),{'final_output':GoalReview(reason='Counts are high',objective_met=True,
            criteria=[CriterionReview(index=0,met=True,finding_ids=['invented-id'],reason='Assume completion')])})()
    monkeypatch.setattr('omnitrix.investigator.Runner.run',run)
    brain=InvestigatorBrain(store)
    assert not (await brain.completion_review(store.snapshot(goal)))['passed']
    assert not (await brain.completion_review(store.snapshot(goal)))['passed']
    assert len(calls)==1
    if hasattr(store,'_campaign_client'):await store._campaign_client.close()
