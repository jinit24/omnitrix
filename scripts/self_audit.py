#!/usr/bin/env python3
"""Dogfood on real Omnitrix source: seeded defect triage -> repair -> external checks.

The probes are human-authored regressions, not autonomous discovery. The live manager
chooses its own research plan; Codex writes the patch; independent subprocesses
verify it. No merge/push/PR is performed by this script.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.control import Control
from omnitrix.implementation import AcceptanceCheck, ImplementationSpec, Implementations, git
from omnitrix.reporting import goal_report, record_experiment, write_report
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor


ORACLE = '''import json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,os.environ['OMNITRIX_CANDIDATE'])
from omnitrix.research import Research
from omnitrix.research_models import Evaluation,Finding,ResearchPlan,ResearchReport
from omnitrix.store import Store
report=ResearchReport(findings=[Finding(statement='Database dominates',classification='claim',evidence_ids=['db'])],supporting_evidence=['db'],contradictory_evidence=[],uncertainties=[],recommendation='Investigate',blocked=False)
evaluation=Evaluation(verdict='supported',evidence_ids=['cache'],reason='Cache observed',feedback='Proceed')
result=Research.gate(report,evaluation,[{'id':'db','stale':False},{'id':'cache','stale':False}])
print('unrelated_citation_verdict='+result.verdict)
if '--probe-gate' in sys.argv: sys.exit(0 if result.verdict=='insufficient' else 1)
assert result.verdict=='insufficient','Unrelated evaluator citations accepted'
with tempfile.TemporaryDirectory() as d:
    p=Path(d);(p/'source.md').write_text('source')
    store=Store(p/'state.db');a=store.create_goal('First goal',['Evidence'],p);b=store.create_goal('Second goal',['Evidence'],p)
    service=Research(store);plan=ResearchPlan(scope=['source.md'],question='Which issue should be prioritized?')
    first=service.plan(a,'discovery',plan);second=service.plan(b,'discovery',plan)
    assert not second['duplicate'] and service.job(second['job_id'])['goal_id']==b,'Another goal owns the duplicate job'
print('independent regressions passed')
'''


def prepare(output, ref):
    output.mkdir(parents=True, exist_ok=False)
    repo = output / 'repo'
    subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(ROOT), str(repo)], check=True)
    git(repo, 'checkout', '--detach', ref)
    oracle = output / 'independent_oracle.py'
    oracle.write_text(ORACLE)
    obs = repo / 'observability'
    obs.mkdir()
    rows = []
    logs = []
    for mode in ('--probe-gate', '--all'):
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(oracle), mode], cwd=repo,
            env={**os.environ, 'OMNITRIX_CANDIDATE': str(repo)}, capture_output=True, text=True, timeout=30)
        rows.append(f'{mode},{int(result.returncode == 0)},{(time.monotonic()-started)*1000:.3f}')
        logs.append(f'probe={mode} exit={result.returncode}\n{result.stdout}\n{result.stderr}')
    (obs / 'checks.csv').write_text('probe,passed,elapsed_ms\n' + '\n'.join(rows) + '\n')
    (obs / 'regressions.log').write_text('\n'.join(logs))
    (obs / 'catalog.md').write_text(
        f'Observed against real Omnitrix revision {ref}. These are human-authored regression probes, not autonomous discovery.\n'
        'checks.csv has probe pass/fail counts and wall time; regressions.log has actual process outputs.\n'
        'The gate-only probe checks whether an evaluator can support claims using unrelated citations.\n'
        'The combined probe additionally checks whether identical plans in separate goals return independent jobs.\n'
        'Investigate implementation in omnitrix/research.py, omnitrix/knowledge.py, and contracts in omnitrix/research_models.py.\n'
        'The independent oracle is outside the checkout. Existing tests are in tests/.\n')
    git(repo, 'add', 'observability')
    git(repo, '-c', 'user.name=Omnitrix evaluation', '-c', 'user.email=evaluation@localhost', 'commit', '-m', 'Record independent self-audit observations')
    return repo, oracle


async def run(args):
    output = args.output.resolve()
    repo, oracle = prepare(output, args.ref)
    store = Store(output / 'state.db')
    criteria = ['The independent research-integrity regression oracle passes', 'All existing deterministic tests still pass']
    goal = store.create_goal('Investigate measured research-integrity failures in Omnitrix itself and implement a bounded, independently verified correction.', criteria, repo)
    control = Control(store)
    control.require_implementation(goal)
    proposal = control.propose(goal, 'policy', {'instructions':
        'Use typed discover/research to inspect the source and observability exports. Probes are known failures to triage, not autonomous discovery claims. '
        'After independently supported research, this explicitly authorized harness supplies an implementation ID with changes limited to omnitrix/research.py. '
        'Use implement and audit on that ID. Do not ask for duplicate permission or complete with recommendations only. '
        'Prefer 8 tool calls and 60 seconds per research role. No deployment, push or merge is authorized by this harness.'})
    control.accept(goal, proposal)
    supervisor = await Supervisor(store, goal).open()
    supervisor.autonomy = args.steps
    implementation = None
    result = {'title': 'Omnitrix real-source self-audit', 'mode': 'live-seeded-defect-triage-and-repair',
              'baseline_revision': args.ref, 'goal_id': goal, 'autonomous_discovery': False}
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            accepted = store.db.execute("SELECT id FROM research_jobs WHERE goal_id=? AND status='supported' ORDER BY updated DESC LIMIT 1", (goal,)).fetchone()
            if accepted and implementation is None:
                checks = [AcceptanceCheck(requirement=criteria[0], argv=[sys.executable, str(oracle)], stdout_contains='independent regressions passed'),
                          AcceptanceCheck(requirement=criteria[1], argv=[sys.executable, '-m', 'pytest', '-q', 'tests'], timeout=120)]
                service = Implementations(store, supervisor.runtime)
                spec = ImplementationSpec(objective='Fix the reproduced research-integrity failures without changing source contracts or bypassing independent checks.',
                    research_job=accepted[0], allowed_paths=['omnitrix/research.py'], checks=checks, seconds=300)
                authorization = service.authorize(goal, spec, explicit=args.allow_code_modifications)
                implementation = service.create(authorization)
                supervisor.autonomy = max(supervisor.autonomy, 4)
                store.execute("UPDATE goals SET status='active' WHERE id=?", (goal,))
            if not supervisor.work:
                if supervisor.autonomy < 1:
                    break
                await supervisor.advance()
            await supervisor.monitor(stall_seconds=400)
            if supervisor.status()['goal']['status'] == 'completed':
                break
            await asyncio.sleep(.2)
        result['status'] = supervisor.status()['goal']['status']
    except Exception as exc:
        result.update(status='error', error_type=type(exc).__name__)
    finally:
        await supervisor.close()
        result['seconds'] = round(time.monotonic() - started, 3)
        result['snapshot'] = store.snapshot(goal)
        result['verification'] = [dict(r) for r in store.db.execute('SELECT * FROM verifications')]
        result['audits'] = [dict(r) for r in store.db.execute('SELECT * FROM completion_audits')]
        result['usage'] = [json.loads(r[0]) for r in store.db.execute("SELECT detail FROM activity WHERE kind='usage'")]
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        write_report(goal_report(store, goal), output / 'index.html')
        if args.log:
            record_experiment(args.log, str(output), result)
        print(json.dumps({'status': result['status'], 'seconds': result['seconds'], 'implementation': implementation,
                          'verification_passes': sum(v['passed'] for v in result['verification']), 'audits': result['audits']}))
        store.db.close()
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ref', default=git(ROOT, 'rev-parse', 'HEAD'))
    parser.add_argument('--allow-code-modifications', action='store_true', required=True)
    parser.add_argument('--steps', type=int, default=14)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--log', type=Path, help='Append results to this JSON repair log and regenerate its sibling HTML')
    args = parser.parse_args()
    if not os.getenv('OPENAI_API_KEY'):
        parser.error('OPENAI_API_KEY must be supplied securely in the runtime')
    sys.exit(asyncio.run(run(args)))
