#!/usr/bin/env python3
"""Paired engineering benchmark. Mock mode validates the harness, not LLM quality."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.implementation import Implementations,ImplementationSpec,AcceptanceCheck,git
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.runtime import CodexRuntime
from omnitrix.supervisor import Supervisor
from omnitrix.models import Decision,Handoff
from omnitrix.research_models import DiscoveryReport,MapEntry,Opportunity,ResearchReport,Finding,Evaluation


class BenchmarkAgents:
    def __init__(self,task): self.task=task
    async def run(self,role,context,tools):
        if role=='evaluation':
            evidence=context['evidence']
            ids=[e['id'] for e in evidence if 'FAIL' in e['excerpt']]
            return Evaluation(verdict='supported' if ids else 'insufficient',evidence_ids=ids,
                reason='Independent fixture oracle records an unmet criterion' if ids else 'No failing criterion observed',
                feedback='Use the independent executable oracle to verify the candidate')
        source=tools.read('system.md')
        hypothesis='Baseline fails: '+self.task['criteria'][-1]['requirement']
        if role=='discovery':
            return DiscoveryReport(system_map=[MapEntry(name=self.task['name'],kind='service',description='Bounded engineering fixture',
                depends_on=[],evidence_ids=[source['id']],assumption=False)],opportunities=[Opportunity(
                title=self.task['name'],hypothesis=hypothesis,falsification='Oracle records PASS for the stated criterion',
                scope=['system.md','service.py'],impact=4,confidence=5,risk=1,effort=1,evidence_ids=[source['id']],
                rationale='Measured baseline violation of a mandatory criterion')],gaps=['Production-scale behavior is not measured'])
        return ResearchReport(findings=[Finding(statement=hypothesis,classification='claim',evidence_ids=[source['id']])],
            supporting_evidence=[source['id']],contradictory_evidence=[],uncertainties=['Fixture only'],
            recommendation='Implement the bounded contract fix, then execute all acceptance oracles',blocked=False)


class MockCoding:
    """A fixed scripted patch, equally available to both arms in harness-only mode."""
    def __init__(self,solution): self.solution=solution;self.calls=0
    async def start(self,workspace,instructions,**kwargs):
        self.calls+=1
        (Path(workspace)/'service.py').write_text(self.solution)
        if kwargs.get('on_thread'): kwargs['on_thread']('mock-thread')
        return 'mock-thread',f'mock-turn-{self.calls}'
    async def wait(self,*args): return {'status':'completed','items':[{'type':'agentMessage','text':json.dumps({'status':'completed','summary':'Scripted fixture patch','evidence':['service.py'],'next_steps':[]})}]}
    async def close(self): pass


class MockBrain:
    def __init__(self,task): self.task=task
    async def decide(self,state):
        research=state['research']
        if not research:
            return self.decision('discover',ResearchPlan(scope=['system.md','service.py'],question='Which improvement has strongest evidence?'))
        if not any(r['stage']=='research' for r in research):
            return self.decision('research',ResearchPlan(scope=['system.md','service.py'],question='Resolve the opportunity contract',
                hypothesis='Baseline fails: '+self.task['criteria'][-1]['requirement'],falsification='Oracle records PASS for the stated criterion'))
        implementations=state['implementations']
        if not implementations:
            return self.decision('pause')
        implementation=implementations[-1]
        action='complete' if implementation['status']=='completed' else 'audit' if implementation['status']=='ready_for_audit' else 'implement'
        return self.decision(action,implementation_id=implementation['id'])
    def decision(self,action,plan=None,implementation_id=None):
        return Decision(action=action,rationale='Harness-only deterministic manager decision',instructions='Inspect bounded fixture evidence',
                        task_id=None,priority=1,research_plan=plan,implementation_id=implementation_id)


def fixture(task,directory):
    repo=directory/'repo'
    repo.mkdir(parents=True)
    subprocess.run(['git','init','-b','main',str(repo)],check=True,capture_output=True)
    (repo/'service.py').write_text(task['initial'])
    # The executable acceptance oracle lives outside the candidate's worktree.
    checks=[]
    for i,criterion in enumerate(task['criteria']):
        checker=directory/f'check_{i}.py'
        checker.write_text("import os,sys\nsys.path.insert(0,os.environ['OMNITRIX_CANDIDATE'])\nfrom service import process\n"+criterion['code']+'\nprint("criterion passed")\n')
        checks.append(AcceptanceCheck(requirement=criterion['requirement'],argv=[sys.executable,str(checker)],stdout_contains='criterion passed'))
    observations=[]
    for check in checks:
        measured=subprocess.run(check.argv,cwd=repo,env={**os.environ,'OMNITRIX_CANDIDATE':str(repo),'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,timeout=10)
        observations.append(('PASS' if measured.returncode==0 else 'FAIL')+': '+check.requirement)
    contracts='\n\n'.join(c['requirement']+'\n```python\n'+c['code']+'\n```' for c in task['criteria'])
    (repo/'system.md').write_text('Independent baseline observations for '+task['name']+'\n'+'\n'.join(observations)+
        '\n\nAuthoritative acceptance contracts (including injected callback definitions). These are expectations, not implementation code:\n'+contracts+'\n')
    (repo/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    git(repo,'add','.')
    git(repo,'-c','user.name=Benchmark','-c','user.email=benchmark@localhost','commit','-m','Identical task fixture')
    return repo,checks


async def score(candidate,repo,checks,output):
    # Score immutable code, never a potentially dirty or partially edited worker tree.
    candidate=candidate or git(repo,'rev-parse','HEAD^{commit}')
    scoring_tree=(output/'score-worktree').resolve()
    git(repo,'worktree','add','--detach',str(scoring_tree),candidate)
    repo=scoring_tree
    results=[]
    for i,check in enumerate(checks):
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'OMNITRIX_CANDIDATE':str(repo),'PYTHONDONTWRITEBYTECODE':'1'}
        proc=await asyncio.create_subprocess_exec(*check.argv,env=env,cwd=repo,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        try:
            stdout,_=await asyncio.wait_for(proc.communicate(),60)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait();stdout=b'timeout'
        artifact=output/f'score-{i}.log';artifact.write_bytes(stdout)
        results.append({'requirement':check.requirement,'passed':proc.returncode==0 and b'criterion passed' in stdout,'artifact':str(artifact),'candidate':candidate})
    if git(repo,'rev-parse','HEAD')!=candidate or git(repo,'diff','--name-only'):
        for result in results:
            result['passed']=False
            result['error']='Acceptance scoring mutated the candidate'
    return results


async def run_arm(task,arm,directory,live,allow,steps,runtime_override=None,arm_seconds=1200):
    directory.mkdir(parents=True)
    repo,checks=fixture(task,directory)
    initial_tree=git(repo,'rev-parse','HEAD^{tree}')
    started=time.monotonic()
    errors=[];candidate=None;candidate_repo=repo;claims=None
    runtime=runtime_override or (CodexRuntime() if live else MockCoding(task['solution']))
    store=Store(directory/'state.db')
    goal=store.create_goal(task['goal'],[c['requirement'] for c in task['criteria']],repo)
    supervisor=None
    if arm=='manager':
        control=Control(store)
        control.require_implementation(goal)
        proposal=control.propose(goal,'policy',{'instructions':
            'Use bounded discover and hypothesis-driven research for investigations; generic delegate work is not accepted research. '
            'Once research is independently supported, this explicitly authorized benchmark harness creates an implementation ID. '
            'Choose implementation and final audit to satisfy all mandatory executable criteria. '
            'Do not complete on recommendations or spend budget on unobservable production-scale behavior. '
            'The fixture, measured baseline observations and independent acceptance checks define the scope.'})
        control.accept(goal,proposal)
    try:
        if arm=='single':
            prompt=json.dumps({'goal':task['goal'],'criteria':[c.model_dump() for c in checks],
                'instructions':'Authorized to modify only service.py in this isolated fixture. Improve the system, run relevant tests, and report outcome. No deployments or destructive operations.'})
            thread,turn=await runtime.start(str(repo),prompt,write=True,schema=Handoff.model_json_schema())
            result=await runtime.wait(thread,turn,arm_seconds)
            for item in result.get('items',[]):
                if item.get('type')=='agentMessage':
                    try:
                        claims=Handoff.model_validate_json(item['text']).status=='completed'
                    except (ValueError,KeyError):
                        pass
            git(repo,'add','service.py')
            if git(repo,'diff','--cached','--name-only'):
                git(repo,'-c','user.name=Benchmark','-c','user.email=benchmark@localhost','commit','-m','Baseline candidate')
            candidate=git(repo,'rev-parse','HEAD')
        else:
            supervisor=await Supervisor(store,goal,brain=None if live else MockBrain(task),runtime=runtime,
                research=Research(store) if live else Research(store,BenchmarkAgents(task))).open()
            # The real manager chooses discovery/research. This harness does not supply a live plan.
            supervisor.autonomy=steps
            authorized=False
            deadline=started+arm_seconds
            while time.monotonic()<deadline:
                accepted=store.db.execute("SELECT id FROM research_jobs WHERE goal_id=? AND status='supported' ORDER BY updated DESC LIMIT 1",(goal,)).fetchone()
                if accepted and not authorized:
                    service=Implementations(store,runtime)
                    spec=ImplementationSpec(objective=task['goal'],research_job=accepted[0],allowed_paths=['service.py'],checks=checks,seconds=600)
                    approval=service.authorize(goal,spec,explicit=allow)
                    service.create(approval);authorized=True
                    supervisor.autonomy=max(3,supervisor.autonomy)
                    store.execute("UPDATE goals SET status='active' WHERE id=?",(goal,))
                if not supervisor.work:
                    if supervisor.autonomy<1: break
                    await supervisor.advance()
                await supervisor.monitor(stall_seconds=600)
                state=supervisor.status()
                if state['goal']['status']=='completed': break
                await asyncio.sleep(.1)
            row=store.db.execute('SELECT * FROM implementations ORDER BY created DESC LIMIT 1').fetchone()
            if row:
                candidate=row['candidate'];candidate_repo=Path(row['worktree']) if Path(row['worktree']).is_dir() else repo;claims=row['status']=='completed'
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        if supervisor: await supervisor.close()
        else: await runtime.close()
    candidate=candidate or git(repo,'rev-parse','HEAD^{commit}')
    scored=await score(candidate,candidate_repo,checks,directory)
    elapsed=time.monotonic()-started
    queries=[(r[0],r[1]) for r in store.db.execute('SELECT operation,query FROM research_queries')]
    repeated=len(queries)-len(set(queries))
    corrections=store.db.execute('SELECT COALESCE(sum(max(attempts-1,0)),0) FROM implementations').fetchone()[0]
    usage=[json.loads(r[0]) for r in store.db.execute("SELECT detail FROM activity WHERE kind='usage'")]
    tokens=None
    runtime_usage=getattr(runtime,'usage',{})
    if usage or runtime_usage:
        tokens={'input':sum(u.get('input_tokens',0) for u in usage)+sum(u.get('inputTokens',0) for u in runtime_usage.values()),'output':sum(u.get('output_tokens',0) for u in usage)+sum(u.get('outputTokens',0) for u in runtime_usage.values())}
    report={'task':task['name'],'fixture_version':2,'arm':arm,'goal_id':goal,'database':str(store.path),'initial_tree':initial_tree,'arm_seconds':arm_seconds,'manager_model':os.getenv('OMNITRIX_MODEL','gpt-4.1'),'codex_model':os.getenv('OMNITRIX_CODEX_MODEL','runtime default'),'mode':'live' if live else 'mock-harness-only','candidate':candidate,
        'requirements_satisfied':sum(r['passed'] for r in scored),'requirements_total':len(scored),'checks':scored,
        'wall_seconds':elapsed,'tokens':tokens,'token_measurement_complete':bool(live and not errors and getattr(runtime,'session_ids',set()) and set(runtime.usage)==runtime.session_ids),
        'repeated_research_queries':repeated if arm=='manager' else None,'corrections':corrections if arm=='manager' else None,
        'interventions':store.db.execute("SELECT count(*) FROM messages WHERE direction='user_to_manager' AND kind='control' AND state='acknowledged'").fetchone()[0],'incorrect_completion_claim':bool(claims and not all(r['passed'] for r in scored)) if claims is not None else None,
        'recovery_after_interruption':None,'selected_improvement_quality':sum(r['passed'] for r in scored)/len(scored),
        'baseline_requirements_satisfied':repo.joinpath('system.md').read_text().count('PASS:'),
        'verified_outcome_gain':sum(r['passed'] for r in scored)-repo.joinpath('system.md').read_text().count('PASS:'),
        'selected_opportunities':[r['title'] for r in store.db.execute('SELECT title FROM opportunities ORDER BY score DESC')],
        'verified_outcome':all(r['passed'] for r in scored),'errors':errors,
        'limitations':['Mock mode measures only the fixture and orchestration harness.' if not live else 'Small fixture suite; not production evidence.',
                      'Internal worker research/corrections are unobservable in baseline unless explicitly reported.',
                      'Unmeasured token totals and recovery are null, never imputed as zero.']}
    (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    store.db.close()
    return report


async def main(args):
    if args.live and not args.allow_code_modifications:
        raise SystemExit('Live coding requires --allow-code-modifications for isolated benchmark fixtures')
    if args.live and not os.getenv('OPENAI_API_KEY'):
        raise SystemExit('Set OPENAI_API_KEY securely for live manager calls; authenticate Codex separately')
    tasks=json.loads((ROOT/'benchmarks/tasks.json').read_text())
    args.output=args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=True)
    results=[];trial=0;deadline=time.monotonic()+args.hours*3600
    while True:
        for task in tasks:
            # Alternate order to reduce consistent warm-up bias across repeated trials.
            for arm in (('single','manager') if trial%2==0 else ('manager','single')):
                target=args.output/f'trial-{trial}'/task['name']/arm
                if (target/'result.json').exists():
                    results.append(json.loads((target/'result.json').read_text()));continue
                if target.exists():
                    raise SystemExit(f'Incomplete arm at {target}; inspect its DB/worktrees before rerunning. No blind replay.')
                results.append(await run_arm(task,arm,target,args.live,args.allow_code_modifications or not args.live,args.steps,arm_seconds=args.arm_seconds))
                (args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        trial+=1
        if args.hours==0 or time.monotonic()>=deadline: break
    print(json.dumps({'mode':'live' if args.live else 'mock-harness-only','runs':len(results),
        'requirements_satisfied':sum(r['requirements_satisfied'] for r in results),
        'incorrect_completion_claims':sum(r['incorrect_completion_claim'] is True for r in results),
        'report':str(args.output/'results.json'),'architecture_superiority':'not established'},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true')
    p.add_argument('--allow-code-modifications',action='store_true')
    p.add_argument('--hours',type=float,default=0)
    p.add_argument('--steps',type=int,default=12)
    p.add_argument('--arm-seconds',type=int,default=1200)
    p.add_argument('--output',type=Path,default=ROOT/'.omnitrix/benchmark')
    args=p.parse_args()
    if args.hours<0 or not 1<=args.steps<=1000 or not 10<=args.arm_seconds<=3600: p.error('hours must be nonnegative; steps must be 1..1000')
    asyncio.run(main(args))
