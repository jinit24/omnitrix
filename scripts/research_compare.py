#!/usr/bin/env python3
"""Metered research comparisons: same workload, tools, time and dollar ceilings.

The single-Codex arm uses GPT-5.3-Codex through Responses with local evidence
tools. It is not Codex CLI. Single GPT-4.1 separates model and architecture effects.
These are controlled regression cases, not held-out production benchmarks.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import csv
import hashlib
from openai import APIConnectionError,APITimeoutError,InternalServerError,RateLimitError
from agents.exceptions import ModelBehaviorError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agents import Agent,Runner,RunConfig,ModelSettings
from openai.types.shared import Reasoning
from omnitrix.campaign import Campaign
from omnitrix.frontier import Frontier
from omnitrix.knowledge import ResearchTools
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan,ResearchReport
from omnitrix.reporting import goal_report,record_experiment,write_report
from omnitrix.resources import list_resources
from omnitrix.spending import Spending,SpendingLimitReached,metered_model
from omnitrix.investigator import evidence_tools
from omnitrix.store import Store
from research_eval import fixture,CASES

ARMS=('omnitrix','investigator','single-gpt4.1','single-codex')
OBJECTIVE='Identify the dominant measured system cost and a falsifiable next action. Compare independent explanations using available code, logs and metrics. Report component totals with units and captured citations; separate controlled workload facts from unmeasured production effects.'


def normalized(text):return text.casefold().replace('_',' ').replace('-',' ')


def accepted_report(store,goal):
    """The campaign publishes its full verified knowledge, not only its last subquestion."""
    findings=[json.loads(row[0])['finding'] for row in store.db.execute("SELECT body FROM knowledge WHERE goal_id=? AND kind='finding' AND classification='verified' ORDER BY created",(goal,))]
    return {'findings':findings,'recommendation':'Inspect the final manager decision and verified knowledge for next actions'} if findings else None


def score(store,goal,oracle,report):
    evidence={row['id']:dict(row) for row in store.db.execute('''SELECT DISTINCT s.* FROM research_sources s
        JOIN research_access a ON a.source_id=s.id JOIN research_runs r ON r.id=a.run_id
        JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?''',(goal,))}
    observed={row['locator'] for row in evidence.values()}
    target=normalized(oracle['target']);expected=oracle['cost_ms'][oracle['target']]
    claims=[f for f in (report or {}).get('findings',[]) if f['classification']=='claim'
        and f['evidence_ids'] and all(s in evidence for s in f['evidence_ids'])]
    target_claims=[f for f in claims if target in normalized(f['statement'])]
    def correct_number(finding):
        for number,unit in re.findall(r'(\d[\d,]*(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|s)\b',finding['statement'],re.I):
            value=float(number.replace(',',''))*(1000 if unit.casefold() in ('s','second','seconds') else 1)
            if abs(value-expected)<max(1,expected*.001):return True
        return False
    triangulated=any({'code','logs','metrics'}.issubset({
        'metrics' if evidence[s]['locator'].endswith('.csv') else 'logs' if evidence[s]['locator'].endswith('.log') else 'code'
        for s in finding['evidence_ids']}) for finding in target_claims)
    queries=[dict(r) for r in store.db.execute('''SELECT q.* FROM research_queries q JOIN research_runs r ON r.id=q.run_id
        JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?''',(goal,))]
    checks={
        'observed_multiple_code_paths':len([p for p in observed if p.startswith('src/')])>=2,
        'used_metrics':'telemetry/cost.csv' in observed,
        'used_logs':'telemetry/worker.log' in observed,
        'reached_log_tail':any(row['locator'].endswith('.log') and 'total_ms' in row['excerpt'] for row in evidence.values()),
        'aggregated_later_rows':any(q['operation']=='summarize_csv' and json.loads(q['query'])[3]>1 for q in queries),
        'reported_oracle_component':bool(target_claims),
        'reported_oracle_numeric_total':any(correct_number(f) for f in target_claims),
        'claim_cites_code_logs_metrics':triangulated,
    }
    return {'passed':sum(checks.values()),'total':len(checks),'checks':checks,'oracle':oracle,
        'limitation':'Observable coverage, target/number matching and citation-kind checks, not a semantic proof. No production/general superiority claim.'}


async def single(store,goal,model):
    frontier=Frontier(store,goal);frontier.build()
    paths=[r[0] for r in store.db.execute('SELECT locator FROM frontier_files WHERE goal_id=?',(goal,))]
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['README.md'],question='Which measured system cost warrants investigation?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('single',?,'running',?)",(job,time.time()))
    tools=ResearchTools(store,'single',store.snapshot(goal)['goal']['workspace'],paths,80)
    settings=ModelSettings(max_tokens=3500,tool_choice='required')
    if model=='gpt-5.3-codex':settings.reasoning=Reasoning(effort='low')
    agent=Agent(name='Single research agent',model=metered_model(store,goal,'single',model),
        instructions=(ROOT/'instructions/RESEARCH.md').read_text()+'\nYou own the broad investigation, including selecting sources and hypotheses. No hypothesis or prescribed plan is supplied. Return at most four concise, citable findings and a concrete next action.',
        tools=evidence_tools(tools,frontier),output_type=ResearchReport,model_settings=settings)
    for attempt in range(3):
        try:
            result=await Runner.run(agent,json.dumps({'goal':OBJECTIVE,'root_inventory':list_resources(tools.workspace),
                'prior_observations':tools.evidence()[-12:] if attempt else []}),
                max_turns=40,run_config=RunConfig(tracing_disabled=True))
            break
        except (APIConnectionError,APITimeoutError,InternalServerError,RateLimitError,ModelBehaviorError) as exc:
            from omnitrix.control import Control
            Control(store).event(goal,'baseline_recovery',{'attempt':attempt+1,'error':type(exc).__name__,
                'note':'Same three-attempt ceiling as the controller. Unknown-cost reservations stay held; prior read-only evidence remains available.'})
            if attempt==2:raise
            await asyncio.sleep(attempt+1)
    report=result.final_output.model_dump()
    store.execute("UPDATE research_runs SET status='reported',report=?,finished=? WHERE id='single'",(json.dumps(report),time.time()))
    return report


async def arm(directory,case,name,dollars,seconds,variant):
    workspace,oracle=fixture(directory,case)
    if variant in ('renamed-with-history','renamed-history-scaled'):
        replacements={'batch_loader':'record_lookup','upstream':'event_export','cache':'hotset'}
        for path in list(workspace.rglob('*')):
            if not path.is_file():continue
            content=path.read_text()
            for old,new in replacements.items():content=content.replace(old,new)
            path.write_text(content)
        for old,new in replacements.items():
            path=workspace/'src'/f'{old}.py'
            if path.exists():path.rename(path.with_name(f'{new}.py'))
        oracle=oracle|{'target':replacements[oracle['target']],
            'cost_ms':{replacements[k]:v for k,v in oracle['cost_ms'].items()}}
        if variant=='renamed-history-scaled':
            factors={'hotset':1.7,'record_lookup':.6,'event_export':1.1}
            csv_path=workspace/'telemetry/cost.csv'
            rows=list(csv.DictReader(csv_path.read_text().splitlines()))
            totals={key:0.0 for key in factors}
            for row in rows:
                value=float(row['elapsed_ms'])*factors[row['operation']]
                row['elapsed_ms']=f'{value:.6g}';totals[row['operation']]+=value
            with csv_path.open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=['operation','elapsed_ms']);writer.writeheader();writer.writerows(rows)
            log_path=workspace/'telemetry/worker.log';text=log_path.read_text().replace('cost_ms=0.2','cost_ms=0.34')
            text=re.sub(r'(record_lookup calls=600 total_ms=)[\d.]+',lambda m:m[1]+f"{totals['record_lookup']:.6g}",text)
            text=re.sub(r'(event_export total_ms=)[\d.]+',lambda m:m[1]+f"{totals['event_export']:.6g}",text)
            log_path.write_text(text);oracle=oracle|{'cost_ms':totals}
        (workspace/'telemetry/history.csv').write_text('operation,elapsed_ms\nhotset,9000000\n')
        with (workspace/'telemetry/catalog.md').open('a') as stream:
            stream.write('\ntelemetry/history.csv is an archived run from 2019, freshly copied here. It is not part of run=current; never mix these totals.\n')
    with (workspace/'telemetry/catalog.md').open('a') as stream:
        stream.write('\nThe controlled driver exercises component operations independently: 6000 cache probes, 600 database lookups and 600 publishes. Durations are synthetic additive operation costs with no overlap, not one request critical-path trace. Source files illustrate those operation mechanisms; do not infer production speedup.\n')
    # A thousand distractors exercise catalog/page behavior; all arms see identical bytes.
    for n in range(1000):(workspace/f'z_archive_{n:04}.md').write_text('Archived note. No current measurements.\n')
    subprocess.run(['git','init','-q',str(workspace)],check=True)
    subprocess.run(['git','-C',str(workspace),'add','.'],check=True)
    subprocess.run(['git','-C',str(workspace),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','Controlled workload'],check=True)
    store=Store(directory/'state.db');goal=store.create_goal(OBJECTIVE,['Use captured evidence and state workload limitations'],workspace)
    model='gpt-5.3-codex' if name in ('single-codex','investigator') else 'gpt-4.1-2025-04-14'
    result={'name':case+'/'+name,'mode':'live-controlled-comparison','goal_id':goal,'model':model,'architecture':name,'variant':variant,
        'budget_usd':dollars,'time_limit_seconds':seconds,'native_codex_cli':False,'status':'running'}
    started=time.monotonic();report=None
    try:
        if name in ('omnitrix','investigator'):
            campaign=Campaign(store,goal);campaign.configure(days=seconds/86400,decisions=20,research_runs=80,dollars=dollars,
                quality={'sources':5,'branches':2,'hypotheses':1,'mechanisms':0},strategy='investigator' if name=='investigator' else 'manager')
            status=await asyncio.wait_for(campaign.drive(),seconds+1)
            result['status']=status['state']
            report=accepted_report(store,goal)
        else:
            Spending(store,goal).configure(dollars,model)
            report=await asyncio.wait_for(single(store,goal,model),seconds)
            result['status']='reported'
    except SpendingLimitReached:
        result['status']='dollar_budget'
    except asyncio.TimeoutError:
        result['status']='time_budget'
    except Exception as exc:
        result.update(status='error',error_type=type(exc).__name__)
    finally:
        if hasattr(store,'_campaign_client'):await store._campaign_client.close()
        result.update(seconds=round(time.monotonic()-started,3),spending=Spending(store,goal).status(),score=score(store,goal,oracle,report),report=report,
            actual_models=[r[0] for r in store.db.execute('SELECT DISTINCT model FROM spending_requests WHERE goal_id=?',(goal,))])
        (directory/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        write_report(goal_report(store,goal),directory/'goal.html')
        store.db.close()
    print(json.dumps({k:result[k] for k in ['name','status','seconds','spending','score']}),flush=True)
    return result


async def main(args):
    cases=args.case or list(CASES);arms=args.arm or list(ARMS)
    if not 0<args.dollars<=25 or args.dollars*len(cases)*len(arms)>25:
        raise ValueError('Combined maximum reservation ceilings for this comparison must not exceed $25')
    args.output.mkdir(parents=True,exist_ok=False)
    source_files=sorted((ROOT/'omnitrix').glob('*.py'))+sorted((ROOT/'instructions').glob('*.md'))+[Path(__file__)]
    config={'cases':cases,'arms':arms,'dollars_per_arm':args.dollars,'combined_ceiling_usd':args.dollars*len(cases)*len(arms),'seconds_per_arm':args.seconds,'variant':args.variant,
        'code_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'source_sha256':{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files},
        'score_version':'all-verified-findings-v1'}
    (args.output/'protocol.json').write_text(json.dumps(config,indent=2)+'\n')
    results=await asyncio.gather(*(arm(args.output/case/name,case,name,args.dollars,args.seconds,args.variant) for case in cases for name in arms))
    data={'title':'Equal-budget controlled research comparison','protocol':config,
        'limitations':['Two controlled regression cases, not held-out workload families.','Single Codex uses Responses API with local tools, not native Codex CLI.','Single GPT-4.1 controls for coordinator/model confounding.','Each arm has the same task, source bytes, available evidence tools, wall limit and maximum dollar ceiling.','Observed coverage and numeric/citation checks do not prove causal correctness or superiority on public repositories.'],
        'runs':results}
    (args.output/'results.json').write_text(json.dumps(data,indent=2)+'\n');write_report(data,args.output/'index.html')
    if args.log:
        for result in results:record_experiment(args.log,str(args.output/result['name']),result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case',action='append',choices=CASES)
    parser.add_argument('--arm',action='append',choices=ARMS)
    parser.add_argument('--dollars',type=float,default=1.5)
    parser.add_argument('--seconds',type=int,default=360)
    parser.add_argument('--variant',choices=('original','renamed-with-history','renamed-history-scaled'),default='original')
    parser.add_argument('--log',type=Path)
    asyncio.run(main(parser.parse_args()))
