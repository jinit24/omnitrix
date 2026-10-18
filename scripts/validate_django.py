"""Bounded, explicitly seeded Django integration experiment; real model decisions."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.supervisor import Supervisor
from omnitrix.implementation import Implementations, ImplementationSpec, AcceptanceCheck, git

async def main(args):
    output = args.output.resolve()
    pristine = args.checkout.resolve()
    if git(pristine, 'status', '--porcelain'):
        raise SystemExit('Use a clean upstream checkout so the external oracle matches its recorded revision')
    output.mkdir(parents=True, exist_ok=False)
    repo = output / 'seeded'
    subprocess.run(['git', 'clone', '--shared', str(pristine), str(repo)], check=True, capture_output=True)
    upstream = git(repo, 'rev-parse', 'HEAD')
    source = repo / 'django/utils/datastructures.py'
    text = source.read_text()
    assert text.count('return reversed(self.dict)') == 1
    assert text.count('return self._getlist(key, default, force_list=True)') == 1
    source.write_text(text.replace('return reversed(self.dict)', 'return iter(self.dict)').replace(
        'return self._getlist(key, default, force_list=True)',
        'return self._getlist(key, default, force_list=False)'))
    oracle = output / 'check.py'
    oracle.write_text('import os, runpy, sys\nfrom pathlib import Path\n'
        'candidate = Path(os.environ["OMNITRIX_CANDIDATE"]).resolve()\n'
        f'tests = Path({str(pristine / "tests")!r})\n'
        'sys.path[:0] = [str(candidate), str(tests)]\n'
        'os.chdir(candidate)\n'
        'mode = sys.argv[1]\n'
        'labels = {"ordered": ["utils_tests.test_datastructures.OrderedSetTests"], '
        '"values": ["utils_tests.test_datastructures.MultiValueDictTests"], '
        '"regression": ["utils_tests.test_datastructures", "httpwrappers"]}[mode]\n'
        'sys.argv = [str(tests / "runtests.py"), *labels, "--settings=test_sqlite", "--parallel=1", "--noinput"]\n'
        'runpy.run_path(str(tests / "runtests.py"), run_name="__main__")\n')
    requirements = [
        'OrderedSet reversal preserves reverse insertion order',
        'MultiValueDict.getlist returns independent lists while preserving internal mutation and default semantics',
        'The Django data-structure and HTTP-wrapper regression tests all pass',
    ]
    checks = [AcceptanceCheck(requirement=requirement, argv=[sys.executable, str(oracle), mode], timeout=120)
              for requirement, mode in zip(requirements, ('ordered', 'values', 'regression'))]
    baseline = []
    for check in checks:
        result = subprocess.run(check.argv, env={**os.environ, 'OMNITRIX_CANDIDATE': str(repo)},
                                capture_output=True, text=True, timeout=120)
        log = result.stdout + result.stderr
        (output / f'baseline-{check.argv[-1]}.log').write_text(log)
        baseline.append({'requirement': check.requirement, 'exit_code': result.returncode, 'output': log})
    assert all(item['exit_code'] != 0 for item in baseline)
    (repo / 'VALIDATION.md').write_text(
        '# Isolated seeded regression exercise\n\n'
        'This checkout intentionally contains two regressions; these are not upstream Django bugs.\n'
        'Restore the behavior established by existing upstream tests. Only django/utils/datastructures.py may change.\n'
        'Useful sources: django/utils/datastructures.py, tests/utils_tests/test_datastructures.py, '
        'tests/httpwrappers/tests.py. Independent checks run unchanged tests from a separate upstream checkout.\n'
        'The user explicitly authorized this test. The harness creates implementation authorization '
        'after research is accepted; the manager cannot grant it.\n\n'
        + '\n'.join('- ' + requirement for requirement in requirements)
        + '\n\n## Observed failing tests\n' + baseline[-1]['output'])
    git(repo, 'add', 'django/utils/datastructures.py', 'VALIDATION.md')
    git(repo, '-c', 'user.name=Omnitrix validation', '-c', 'user.email=validation@localhost',
        'commit', '-m', 'Seed two data structure regressions for isolated validation')
    initial = git(repo, 'rev-parse', 'HEAD')
    store = Store(output / 'state.db')
    goal = store.create_goal('Restore Django data-structure contracts described in VALIDATION.md and verify the unchanged upstream tests',
                             requirements, repo)
    control = Control(store)
    control.require_implementation(goal)
    proposal = control.propose(goal, 'policy', {'instructions':
        'This is an explicitly authorized isolated repair experiment. VALIDATION.md contains the known regression '
        'observations and source paths. Establish the bounded defect using existing source and test contracts. '
        'The harness creates an implementation ID after accepted research. Execute implementation and audit. '
        'No production-performance claims, upstream pushes or deployments. Optional unspecified edge cases '
        'do not expand mandatory criteria. Preserve existing test files.'})
    control.accept(goal, proposal)
    supervisor = await Supervisor(store, goal).open()
    supervisor.autonomy = 12
    started = time.monotonic()
    authorized = False
    restarted = False
    all_runtime_usage = {}
    events = []
    errors = []
    stopped_for_budget = False

    def usage():
        roles = [json.loads(row[0]) for row in store.db.execute("SELECT detail FROM activity WHERE kind='usage'")]
        all_runtime_usage.update(supervisor.runtime.usage)
        sdk_input = sum(row.get('input_tokens', 0) for row in roles)
        sdk_output = sum(row.get('output_tokens', 0) for row in roles)
        native_input = sum(row.get('inputTokens', 0) for row in all_runtime_usage.values())
        native_output = sum(row.get('outputTokens', 0) for row in all_runtime_usage.values())
        # Standard public rates; count cached input at the full rate as a conservative estimate.
        input_rate, output_rate = (2.5, 15) if os.environ['OMNITRIX_MODEL'] == 'gpt-5.4' else (2, 8)
        dollars = (sdk_input * input_rate + sdk_output * output_rate + native_input * 1.75 + native_output * 14) / 1_000_000
        return {'sdk_input': sdk_input, 'sdk_output': sdk_output,
                'native_input': native_input, 'native_output': native_output, 'estimated_usd_no_cache_discount': dollars}

    async def budget_guard():
        nonlocal stopped_for_budget
        while True:
            # Stop far below the $25 ceiling to reserve capacity for an in-flight call.
            if usage()['estimated_usd_no_cache_discount'] >= args.stop_at_usd:
                stopped_for_budget = True
                supervisor.autonomy = 0
                for task in list(supervisor.work.values()):
                    task.cancel()
                return
            await asyncio.sleep(.2)

    guard = asyncio.create_task(budget_guard())
    try:
        while time.monotonic() - started < args.seconds and not stopped_for_budget:
            if not supervisor.work:
                accepted = store.research_state(goal)['accepted_job_ids']
                if accepted and not authorized:
                    service = Implementations(store, supervisor.runtime)
                    spec = ImplementationSpec(objective='Restore the three explicit Django data-structure acceptance criteria',
                        research_job=accepted[0], allowed_paths=['django/utils/datastructures.py'],
                        checks=checks, max_attempts=2, seconds=300)
                    authorization = service.authorize(goal, spec, explicit=True)
                    implementation = service.create(authorization)
                    authorized = True
                    events.append({'event': 'authorized', 'implementation': implementation})
                    print(json.dumps(events[-1]), flush=True)
                if accepted and not restarted:
                    remaining = supervisor.autonomy
                    before = store.research_state(goal)
                    usage()
                    await supervisor.close()
                    store.db.close()
                    store = Store(output / 'state.db')
                    supervisor = await Supervisor(store, goal).open()
                    supervisor.autonomy = remaining
                    assert store.research_state(goal) == before
                    restarted = True
                    events.append({'event': 'restart_after_accepted_research', 'state_restored': True})
                    print(json.dumps(events[-1]), flush=True)
                if supervisor.autonomy < 1:
                    break
                outcome = await supervisor.advance()
                events.append({'event': 'advance', 'outcome': outcome, 'usage': usage()})
                print(json.dumps(events[-1]), flush=True)
            await supervisor.monitor(stall_seconds=300)
            if supervisor.status()['goal']['status'] == 'completed':
                break
            await asyncio.sleep(.2)
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        guard.cancel()
        await asyncio.gather(guard, return_exceptions=True)
        final_usage = usage()
        await supervisor.close()
    state = store.snapshot(goal)
    verifications = [dict(row) for row in store.db.execute('SELECT * FROM verifications')]
    report = {'upstream': 'https://github.com/django/django', 'upstream_revision': upstream,
        'tracked_files': len(git(pristine, 'ls-files').splitlines()), 'seeded_revision': initial,
        'goal_id': goal, 'manager_model': os.environ['OMNITRIX_MODEL'], 'worker_model': os.environ['OMNITRIX_CODEX_MODEL'],
        'seeded_defects': ['OrderedSet.__reversed__ returns insertion order', 'MultiValueDict.getlist returns its mutable backing list'],
        'baseline': baseline, 'goal_status': state['goal']['status'], 'decisions': state['decisions'],
        'research': store.research_history(goal), 'implementations': state['implementations'],
        'verifications': verifications, 'audits': [dict(row) for row in store.db.execute('SELECT * FROM completion_audits')],
        'restart_after_accepted_research': restarted, 'wall_seconds': time.monotonic() - started,
        'usage': final_usage, 'stop_at_usd': args.stop_at_usd,
        'stopped_for_budget': stopped_for_budget, 'errors': errors,
        'events': events, 'limitations': ['Seeded defects, not newly discovered upstream bugs.',
            'One bounded task in a large checkout, not a multi-hour or whole-repository trial.',
            'Restart between stages, not a kill during an in-flight external action.',
            'Usage-based cost estimate without cache discount; not an invoice. Failed calls may omit usage.']}
    for item in state['implementations']:
        if item.get('candidate'):
            report['candidate_patch'] = git(repo, 'diff', initial, item['candidate'], '--', 'django/utils/datastructures.py')
    (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'result': str(output / 'result.json'), 'status': report['goal_status'], 'usage': final_usage, 'errors': errors}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True, help='Clean Django stable/5.2.x checkout')
    parser.add_argument('--output', type=Path, required=True, help='New experiment directory; incomplete runs are never replayed')
    parser.add_argument('--model', choices=['gpt-4.1', 'gpt-5.4'], default='gpt-5.4')
    parser.add_argument('--seconds', type=int, default=900)
    parser.add_argument('--stop-at-usd', type=float, default=3,
                        help='Usage-based stop threshold, at most $3; reserve headroom for in-flight responses')
    parser.add_argument('--allow-code-modifications', action='store_true')
    args = parser.parse_args()
    if not args.allow_code_modifications:
        parser.error('--allow-code-modifications is required for the isolated fixture')
    if not 0 < args.stop_at_usd <= 3 or not 0 < args.seconds <= 1800:
        parser.error('Use a stop threshold in (0, 3] dollars and a wall limit in (0, 1800] seconds')
    if not os.getenv('OPENAI_API_KEY'):
        parser.error('Set OPENAI_API_KEY securely for the live model calls')
    os.environ['OMNITRIX_MODEL'] = args.model
    os.environ['OMNITRIX_CODEX_MODEL'] = 'gpt-5.3-codex'
    os.environ['OMNITRIX_CODEX_REASONING_EFFORT'] = 'medium'
    asyncio.run(main(args))
