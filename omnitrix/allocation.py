"""Bounded exploitation prevents an unlimited sequence of reconnaissance maps."""
import json

from .knowledge import Knowledge, source_path
from .research_models import ResearchBudget, ResearchPlan
from .resources import resource_kind


def allocation(store,goal_id):
    recent=store.db.execute('SELECT stage FROM research_jobs WHERE goal_id=? ORDER BY created DESC LIMIT 3',(goal_id,)).fetchall()
    streak=0
    for row in recent:
        if row['stage']!='discovery':break
        streak+=1
    knowledge=Knowledge(store);leads=[]
    workspace=store.db.execute('SELECT workspace FROM goals WHERE id=?',(goal_id,)).fetchone()[0]
    tested=set()
    for row in store.db.execute("SELECT spec FROM research_jobs WHERE goal_id=? AND stage='research' AND status NOT IN ('invalidated','stale')",(goal_id,)):
        spec=json.loads(row[0]);tested.add((' '.join(spec['hypothesis'].casefold().split()),tuple(sorted(spec['scope']))))
    rows=store.db.execute("""SELECT * FROM opportunities WHERE goal_id=? AND status='open'
        AND json_extract(spec,'$.category') IN ('mechanism','measurement')
        ORDER BY score DESC,created LIMIT 200""",(goal_id,)).fetchall()
    for row in rows:
        spec=json.loads(row['spec'])
        if not spec['confidence'] or (spec['category']=='mechanism' and not (spec.get('workload') or '').strip()):continue
        if spec['category']=='mechanism' and not any(resource_kind(path)=='code' for path in spec['scope']):continue
        if (' '.join(spec['hypothesis'].casefold().split()),tuple(sorted(spec['scope']))) in tested:continue
        try:
            for path in spec['scope']:source_path(workspace,path)
            sources=[knowledge.source(s) for s in spec['evidence_ids']]
        except (OSError,ValueError):continue
        if not sources or any(s['stale'] for s in sources):continue
        leads.append(dict(row)|{'spec':spec})
        if len(leads)==8:break
    return {'recent_discovery_streak':streak,'ready_leads':leads,'recommended_action':'research' if streak>=3 and leads else 'manager_choice',
            'note':'After three discovery jobs, test an existing cited mechanism/measurement lead. Evidence gaps are not inefficiency candidates. This bounds reconnaissance; it does not prove findings or optimal allocation.'}


def rebalance(store,goal_id,decision):
    if decision.action!='discover':return decision
    state=allocation(store,goal_id)
    if state['recommended_action']!='research':return decision
    lead=state['ready_leads'][0];spec=lead['spec']
    plan=ResearchPlan(scope=spec['scope'],question='Test the current candidate against its stated workload and counterevidence: '+spec['title'],
        hypothesis=spec['hypothesis'],falsification=spec['falsification'],opportunity_id=lead['id'],
        budget=ResearchBudget(tool_calls=24,seconds=180,attempts=2))
    revised=decision.model_copy(update={'action':'research','research_plan':plan,
        'rationale':'Allocation controller: three reconnaissance jobs have produced an eligible lead; spend a bounded test on it before another map.',
        'instructions':f"Test {spec['title']}. Workload: {spec.get('workload')}. Look for counterexamples and measure/derive the resource cost; do not turn missing data into a false verdict."})
    from .control import Control
    Control(store).event(goal_id,'research_allocation',{'proposed':decision.model_dump(),'dispatched':revised.model_dump(),'opportunity_id':lead['id']})
    return revised
