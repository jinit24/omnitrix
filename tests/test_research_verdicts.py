import pytest

from omnitrix.knowledge import ResearchTools
from omnitrix.research import Research
from omnitrix.research_models import Evaluation, Finding, ResearchPlan, ResearchReport
from omnitrix.store import Store


def fixture(tmp_path):
    for name in ('a.py','b.py'):
        (tmp_path/name).write_text('def work():\n    return 1\n')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Find bottlenecks',['Use evidence'],tmp_path)
    plan=ResearchPlan(scope=['a.py','b.py'],question='Are quality counters absent in both sources?',
        hypothesis='Both sources contain no quality counter',falsification='A quality counter is present')
    job=Research(store).plan(goal,'research',plan)['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('r',?,'running',0)",(job,))
    return plan,ResearchTools(store,'r',str(tmp_path),plan.scope,20)


def report(ids):
    return ResearchReport(findings=[Finding(statement='No quality counter was found',classification='claim',evidence_ids=ids)],
        supporting_evidence=ids,contradictory_evidence=[],uncertainties=[],recommendation='Check the complete scope',blocked=False)


def test_partial_scope_cannot_support_whole_scope_absence(tmp_path):
    plan,tools=fixture(tmp_path);a=tools.read('a.py')['id']
    evaluation=Evaluation(verdict='supported',evidence_ids=[a],test_evidence_ids=[a],test_kind='absence',
        reason='No counter in either file',feedback='Proceed')
    result=Research.gate(report([a]),evaluation,tools.evidence(),plan,tools.coverage([a]))
    assert result.verdict=='insufficient' and 'b.py' in result.reason
    b=tools.read('b.py')['id'];evaluation=evaluation.model_copy(update={'evidence_ids':[a,b],'test_evidence_ids':[a,b]})
    assert Research.gate(report([a,b]),evaluation,tools.evidence(),plan,tools.coverage([a,b])).verdict=='supported'


def test_coverage_requires_contiguous_unclipped_reads(tmp_path):
    plan,tools=fixture(tmp_path)
    (tmp_path/'a.py').write_text('x\n'*205)
    first=tools.read('a.py',1,100)['id'];tail=tools.read('a.py',201,100)['id']
    assert not tools.coverage([first,tail])['a.py']['read_complete']
    middle=tools.read('a.py',101,100)['id']
    assert tools.coverage([first,middle,tail])['a.py']['read_complete']


def test_full_literal_search_proves_only_the_literal_and_all_pages_are_needed(tmp_path):
    plan,tools=fixture(tmp_path);(tmp_path/'a.py').write_text('x\n'*5001)
    first=tools.search_source('a.py','quality')['id']
    assert not tools.coverage([first])['a.py']['literal_absence']
    last=tools.search_source('a.py','quality',5001)['id']
    assert tools.coverage([first,last])['a.py']['literal_absence']==['quality']
    b=tools.search_source('b.py','different')['id']
    evaluation=Evaluation(verdict='supported',evidence_ids=[first,last,b],test_evidence_ids=[first,last,b],
        test_kind='literal_absence',literal_query='quality',reason='Literal missing',feedback='Proceed')
    result=Research.gate(report([first,last,b]),evaluation,tools.evidence(),plan,tools.coverage([first,last,b]))
    assert result.verdict=='insufficient' and 'b.py' in result.reason


def test_missing_measurement_does_not_falsify_a_bottleneck(tmp_path):
    plan,tools=fixture(tmp_path);a=tools.read('a.py')['id']
    evaluation=Evaluation(verdict='falsified',evidence_ids=[a],test_evidence_ids=[a],test_kind='absence',
        reason='No comparison benchmark is available',feedback='Investigate another source')
    assert Research.gate(report([a]),evaluation,tools.evidence(),plan,tools.coverage([a])).verdict=='insufficient'


def test_valid_report_claims_do_not_substitute_for_a_hypothesis_test(tmp_path):
    plan,tools=fixture(tmp_path);a=tools.read('a.py')['id']
    evaluation=Evaluation(verdict='supported',evidence_ids=[a],reason='This report claim is correct',feedback='Proceed')
    assert Research.gate(report([a]),evaluation,tools.evidence(),plan).verdict=='insufficient'


@pytest.mark.asyncio
async def test_inconsistent_review_is_corrected_once_without_repeating_research(tmp_path):
    from tests_helpers import FixtureAgents
    (tmp_path/'system.md').write_text('Queue polling interval is 30 seconds')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Check interval',['Use real evidence'],tmp_path)
    class Reviewer(FixtureAgents):
        reviews=0
        research_calls=0
        async def run(self,role,context,tools):
            if role=='research':self.research_calls+=1
            if role=='evaluation':
                self.reviews+=1
                if self.reviews==1:
                    ids=context['report']['supporting_evidence']
                    return Evaluation(verdict='falsified',evidence_ids=ids,test_kind='measurement',test_evidence_ids=ids,
                        reason='The measured interval is 30 seconds, so the claim is supported',feedback='The original label is inconsistent')
            return await super().run(role,context,tools)
    agents=Reviewer();service=Research(store,agents)
    job=service.plan(goal,'research',ResearchPlan(scope=['system.md'],question='What is the queue polling interval?',
        hypothesis='The polling interval is 30 seconds',falsification='The registry reports another interval'))['job_id']
    assert (await service.attempt(job))['status']=='supported'
    assert agents.reviews==2 and agents.research_calls==1
    assert service.job(job)['attempts']==1
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==3
    assert store.db.execute("SELECT count(*) FROM activity WHERE kind='review_gate_rejected'").fetchone()[0]==1


@pytest.mark.asyncio
async def test_genuine_insufficiency_is_not_automatically_retried(tmp_path):
    from tests_helpers import FixtureAgents
    (tmp_path/'system.md').write_text('A polling queue exists; interval is unknown')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Check interval',['Do not guess'],tmp_path)
    service=Research(store,FixtureAgents(verdict='insufficient'))
    job=service.plan(goal,'research',ResearchPlan(scope=['system.md'],question='What is the polling interval?',
        hypothesis='The interval is 30 seconds',falsification='The measured interval differs'))['job_id']
    assert (await service.attempt(job))['status']=='insufficient'
    assert store.db.execute('SELECT spent FROM research_limits').fetchone()[0]==2
    assert not store.db.execute("SELECT 1 FROM activity WHERE kind='review_gate_correction'").fetchone()


def test_tail_reads_end_of_large_log_and_paginates_backwards(tmp_path):
    plan,tools=fixture(tmp_path)
    (tmp_path/'a.py').write_text('noise\n'*9000+'summary cost_ms=24000\n')
    tail=tools.tail('a.py',3)
    assert '9001: summary cost_ms=24000' in tail['excerpt']
    assert tail['start_line']==8999 and tail['next_before']==8999
    older=tools.tail('a.py',3,tail['next_before'])
    assert older['end_line']==8998 and not older['reached_file_end']
    assert not tools.coverage([tail['id'],older['id']])['a.py']['read_complete']


def test_last_csv_page_is_not_a_whole_export(tmp_path):
    (tmp_path/'data.csv').write_text('group,ms\na,10\nb,20\n')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Compare costs',['Use whole export'],tmp_path)
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['data.csv'],question='Which measured group has the highest total?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('r',?,'running',0)",(job,))
    tools=ResearchTools(store,'r',str(tmp_path),['data.csv'],3)
    page=tools.summarize_csv('data.csv','ms','group',2)
    assert not page['summary']['partial'] and not page['summary']['whole_file']
    assert not tools.knowledge.source(page['id'])['observation_scope']['whole_file']
    complete=tools.summarize_csv('data.csv','ms','group',1,complete=True)
    assert complete['summary']['whole_file']


def test_content_search_paginates_hits_without_reading_outside_explicit_scope(tmp_path):
    scope=[f'file{n:02}.py' for n in range(12)]
    for path in scope:(tmp_path/path).write_text('header\n# unrelated\nclass Needle: pass\n')
    (tmp_path/'outside.py').write_text('class Needle: pass\n')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Locate runtime implementations',['Capture actual contents'],tmp_path)
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=scope,question='Where is the implementation named Needle?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('r',?,'running',0)",(job,))
    tools=ResearchTools(store,'r',str(tmp_path),scope,20)
    first=tools.search_scope('Needle');second=tools.search_scope('Needle',first['next_after'])
    hits=first['matches']+second['matches']
    assert len(first['matches'])==8 and len(second['matches'])==4
    assert {hit['locator'] for hit in hits}==set(scope) and second['next_after'] is None
    assert all(hit['line']==3 and hit['matching_excerpt_captured'] and hit['evidence']['id'] for hit in hits)
    assert {source['locator'] for source in tools.evidence()}==set(scope)
    assert tools.remaining==6


def test_empty_content_search_page_has_a_continuation_cursor(tmp_path):
    scope=[f'part{n:03}.py' for n in range(501)]
    for path in scope:(tmp_path/path).write_text('ordinary code\n')
    (tmp_path/scope[-1]).write_text('needle beyond first batch\n')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Locate implementation',['Follow cursors'],tmp_path)
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=[scope[0]],question='Where is a referenced implementation?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('r',?,'running',0)",(job,))
    tools=ResearchTools(store,'r',str(tmp_path),scope,4)
    first=tools.search_scope('needle');assert not first['matches'] and first['next_after']==scope[499]
    second=tools.search_scope('needle',first['next_after']);assert second['matches'][0]['locator']==scope[-1]
