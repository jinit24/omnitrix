import json

from omnitrix.allocation import allocation,rebalance
from omnitrix.models import Decision
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.knowledge import ResearchTools
from omnitrix.store import Store


def test_mapping_forever_is_rebalanced_to_a_cited_test_not_a_data_gap(tmp_path):
    (tmp_path/'service.py').write_text('def load(keys,db):\n    return [db.fetch(k) for k in keys]\n')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Find resource inefficiencies',['Test leads'],tmp_path)
    for n in range(3):
        job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['service.py'],question=f'Which mechanism uncertainty matters in pass {n}?'))['job_id']
        store.execute('INSERT INTO research_runs(id,job_id,status,created) VALUES(?,?,?,?)',(f'r{n}',job,'discovered',n))
    source=ResearchTools(store,'r2',str(tmp_path),['service.py'],1).read('service.py')['id']
    base={'title':'Per-item round trips','hypothesis':'N keys produce N database round trips rather than one',
        'falsification':'The code batches the lookups','scope':['service.py'],'impact':4,'confidence':3,'risk':1,'effort':1,
        'evidence_ids':[source],'rationale':'Known source loop with bounded workload','workload':'N distinct keys'}
    for name,category,score in [('gap','evidence_gap',1000),('lead','mechanism',4)]:
        store.execute('INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?)',(name,goal,job,name,json.dumps(base|{'category':category}),score,'open',0,0))
    decision=Decision(action='discover',rationale='One more map',instructions='Survey again',task_id=None,priority=1,
        research_plan=ResearchPlan(scope=['service.py'],question='Map the loop again before testing it?'))
    assert allocation(store,goal)['recommended_action']=='research'
    revised=rebalance(store,goal,decision)
    assert revised.action=='research' and revised.research_plan.opportunity_id=='lead'
    assert revised.research_plan.hypothesis==base['hypothesis']
    assert store.db.execute("SELECT count(*) FROM activity WHERE kind='research_allocation'").fetchone()[0]==1
    Research(store).plan(goal,'research',revised.research_plan)
    assert allocation(store,goal)['recommended_action']=='manager_choice'


def test_no_forced_problem_when_only_missing_data_is_available(tmp_path):
    (tmp_path/'catalog.md').write_text('Production traces unavailable')
    store=Store(tmp_path/'state.db');goal=store.create_goal('Investigate honestly',['No invented issues'],tmp_path)
    assert allocation(store,goal)['ready_leads']==[]
