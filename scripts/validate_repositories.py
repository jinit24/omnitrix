"""Live multi-repository repair trials with durable restart and completion-loss injection.

The fixed cases and acceptance targets live in benchmarks/oss_cases.json and
benchmarks/reliability_protocol.json. This script never pushes upstream changes.
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
from omnitrix.runtime import CodexRuntime
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor

CASES = json.loads((ROOT / 'benchmarks/oss_cases.json').read_text())
PROTOCOL = json.loads((ROOT / 'benchmarks/reliability_protocol.json').read_text())


def measured_usage(store):
    sdk = [json.loads(row[0]) for row in store.db.execute("SELECT detail FROM activity WHERE kind='usage'")]
    native = {}
    for row in store.db.execute("SELECT detail FROM activity WHERE kind='runtime_usage_snapshot' ORDER BY id"):
        record = json.loads(row[0])
        native[record['threadId']] = record['tokenUsage']['total']
    inputs = sum(row.get('input_tokens', 0) for row in sdk)
    outputs = sum(row.get('output_tokens', 0) for row in sdk)
    native_inputs = sum(row.get('inputTokens', 0) for row in native.values())
    native_outputs = sum(row.get('outputTokens', 0) for row in native.values())
    return {'sdk_input': inputs, 'sdk_output': outputs, 'native_input': native_inputs,
        'native_output': native_outputs, 'estimated_usd_no_cache_discount':
        (inputs * 2.5 + outputs * 15 + native_inputs * 1.75 + native_outputs * 14) / 1_000_000,
        'max_sdk_input_per_response': max((row['input_tokens'] for row in sdk), default=0),
        'native_threads': len(native)}


def fixture(args):
    case = CASES[args.case]
    pristine, output = args.checkout.resolve(), args.output.resolve()
    assert git(pristine, 'rev-parse', 'HEAD') == case['revision'], 'Use the pinned upstream revision'
    assert not git(pristine, 'status', '--porcelain'), 'Upstream checkout must be clean'
    output.mkdir(parents=True, exist_ok=False)
    repo = output / 'seeded'
    subprocess.run(['git', 'clone', '--shared', str(pristine), str(repo)], check=True, capture_output=True)
    oracle = output / 'check.py'
    code = ('import importlib,os,sys\nfrom pathlib import Path\n'
        'sys.dont_write_bytecode=True\nos.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]="1"\n'
        'candidate=Path(os.environ["OMNITRIX_CANDIDATE"]).resolve()\n'
        f'sys.path[:0]=[str(candidate / {case["source_root"]!r}), {str(pristine / "tests")!r}]\n'
        f'assert Path(importlib.import_module({case["package"]!r}).__file__).is_relative_to(candidate)\n'
        'os.chdir(candidate)\n'
        f'labels={case["labels"]!r}[int(sys.argv[1])]\n')
    if args.case == 'django':
        code += ('import runpy\n'
            f'sys.argv=[{str(pristine / "tests/runtests.py")!r},*labels,"--settings=test_sqlite","--parallel=1","--noinput"]\n'
            'runpy.run_path(sys.argv[0],run_name="__main__")\n')
    else:
        code += ('import pytest\n'
            f'raise SystemExit(pytest.main([*[str(Path({str(pristine)!r}) / label) for label in labels],'
            '"-q","-p","no:cacheprovider","--disable-warnings"]))\n')
    oracle.write_text(code)
    checks = [AcceptanceCheck(requirement=requirement,
        argv=[str(args.test_python.absolute()), str(oracle), str(index)], timeout=120)
        for index, requirement in enumerate(case['requirements'])]
    def run_checks(candidate, label):
        results = []
        for index, check in enumerate(checks):
            measured = subprocess.run(check.argv, cwd=candidate,
                env={'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'OMNITRIX_CANDIDATE': str(candidate),
                     'PYTHONDONTWRITEBYTECODE':'1'}, capture_output=True, text=True, timeout=120)
            text = measured.stdout + measured.stderr
            (output / f'{label}-{index}.log').write_text(text)
            results.append({'requirement': check.requirement, 'exit_code': measured.returncode, 'output': text})
        return results
    clean = run_checks(pristine, 'clean')
    assert all(row['exit_code'] == 0 for row in clean), 'Clean upstream oracle must pass before fault injection'
    for name, original, replacement in case['seeds']:
        path = repo / name
        text = path.read_text()
        assert text.count(original) == 1, name
        path.write_text(text.replace(original, replacement))
    baseline = run_checks(repo, 'seeded')
    assert all(row['exit_code'] != 0 for row in baseline), 'Every criterion must detect the seeded defect'
    allowed = sorted({entry[0] for entry in case['seeds']})
    (repo / 'VALIDATION.md').write_text('# Authorized isolated repair experiment\n\n'
        'This checkout contains deliberately seeded regressions, not upstream defects. Restore the existing test contracts.\n'
        'Allowed source changes: ' + ', '.join(allowed) + '\n'
        'Useful sources and tests: ' + ', '.join(case['sources']) + '\n'
        'The harness creates implementation authorization after accepted research. Execute implementation and audit. '
        'Unchanged tests in a separate upstream checkout are the independent oracle. Do not edit tests or push upstream.\n\n'
        + '\n'.join('- ' + item for item in case['requirements']) + '\n\nObserved failing tests:\n' + baseline[-1]['output'])
    git(repo, 'add', *allowed, 'VALIDATION.md')
    git(repo, '-c', 'user.name=Validation', '-c', 'user.email=validation@localhost', 'commit', '-m', 'Seed isolated regressions')
    store = Store(output / 'state.db')
    goal = store.create_goal('Restore the contracts in VALIDATION.md and pass all unchanged upstream acceptance tests',
                             case['requirements'], repo)
    control = Control(store)
    control.require_implementation(goal)
    proposal = control.propose(goal, 'policy', {'instructions':
        'This isolated repair and all reads are explicitly authorized. Read VALIDATION.md for observed defects and scope. '
        'Research should establish concrete current defects and existing contracts. The harness creates implementation '
        'authorization after accepted research. Execute implementation and audit. Preserve tests. No deployments, '
        'production performance claims, or upstream pushes. Optional unspecified edge cases do not expand criteria.'})
    control.accept(goal, proposal)
    metadata = {'case': args.case, 'goal': goal, 'upstream_revision': case['revision'],
        'tracked_files': len(git(pristine, 'ls-files').splitlines()), 'started': time.time(),
        'seeded_revision': git(repo, 'rev-parse', 'HEAD'), 'clean': clean, 'baseline': baseline,
        'checks': [check.model_dump() for check in checks], 'allowed_paths': allowed,
        'fault': args.fault, 'research_checkpoint': False}
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    return store, metadata


class DeliveryLossRuntime(CodexRuntime):
    """Lose one completed response while preserving the real native thread."""
    def __init__(self, store, goal, inject):
        super().__init__()
        self.store, self.goal, self.inject = store, goal, inject

    async def wait(self, thread_id, turn_id, timeout=300):
        result = await super().wait(thread_id, turn_id, timeout)
        if self.inject and result['status'] == 'completed':
            self.inject = False
            Control(self.store).event(self.goal, 'injected_completion_loss', {'thread': thread_id, 'turn': turn_id})
            raise ConnectionError('Injected delivery loss after real worker completion; reconcile exact native turn')
        return result


async def trial(args):
    output = args.output.resolve()
    if args.resume:
        metadata = json.loads((output / 'metadata.json').read_text())
        if not metadata['research_checkpoint'] or (output / 'result.json').exists():
            raise SystemExit('Resume requires the saved research checkpoint and no final result')
        store = Store(output / 'state.db')
    else:
        store, metadata = fixture(args)
    goal = metadata['goal']
    runtime = DeliveryLossRuntime(store, goal, metadata['fault'] == 'lost_completion')
    supervisor = await Supervisor(store, goal, runtime=runtime).open()
    control = Control(store)
    if args.resume:
        assert store.research_state(goal) == metadata['saved_research_state']
        assert supervisor.autonomy == metadata['saved_remaining_decisions']
        control.event(goal, 'process_restart_verified', {'remaining_decisions': supervisor.autonomy})
    else:
        supervisor.autonomy = 12
    errors, recovery = [], []
    stopping = False
    def dollars():
        prior = (PROTOCOL['prior_testing_estimate_and_reserve_usd']
                 + PROTOCOL.get('reserved_future_campaign_usd', 0))
        for path in output.parent.glob('*/result.json'):
            if path.parent != output:
                prior += json.loads(path.read_text())['usage']['estimated_usd_no_cache_discount']
        return prior + measured_usage(store)['estimated_usd_no_cache_discount']
    async def guard():
        nonlocal stopping
        while True:
            if (measured_usage(store)['estimated_usd_no_cache_discount'] >= PROTOCOL['per_run_observed_cost_stop_usd']
                or dollars() >= PROTOCOL['cumulative_observed_stop_including_prior_usd']):
                stopping = True
                supervisor.autonomy = 0
                for task in list(supervisor.work.values()): task.cancel()
                return
            await asyncio.sleep(.2)
    budget_guard = asyncio.create_task(guard())
    checkpoint = False
    try:
        while time.time() - metadata['started'] < 600 and not stopping:
            if not supervisor.work:
                accepted = store.research_state(goal)['accepted_job_ids']
                if accepted and not store.db.execute('SELECT 1 FROM implementations').fetchone():
                    service = Implementations(store, runtime)
                    spec = ImplementationSpec(objective=store.snapshot(goal)['goal']['objective'], research_job=accepted[0],
                        allowed_paths=metadata['allowed_paths'], checks=metadata['checks'], max_attempts=2, seconds=300)
                    service.create(service.authorize(goal, spec, explicit=True))
                if accepted and not metadata['research_checkpoint']:
                    metadata.update(research_checkpoint=True, saved_research_state=store.research_state(goal),
                                    saved_remaining_decisions=supervisor.autonomy)
                    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2))
                    checkpoint = True
                    break
                uncertain = store.db.execute("SELECT task_id FROM task_runtime WHERE state='uncertain'").fetchall()
                if uncertain:
                    for row in uncertain:
                        outcome = await supervisor.reconcile(row[0])
                        recovery.append(outcome)
                        print(json.dumps({'event':'reconciled', 'outcome':outcome.get('implementation',{})}),flush=True)
                    if store.db.execute("SELECT 1 FROM task_runtime WHERE state='uncertain'").fetchone():
                        break
                if supervisor.autonomy < 1: break
                outcome = await supervisor.advance()
                print(json.dumps({'event':'advance', 'outcome':outcome, 'usage':measured_usage(store)}),flush=True)
            if supervisor.status()['goal']['status'] == 'completed': break
            await asyncio.sleep(.2)
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        budget_guard.cancel()
        await asyncio.gather(budget_guard, return_exceptions=True)
        await supervisor.close(preserve_autonomy=checkpoint)
    if checkpoint:
        print(json.dumps({'event':'checkpoint_exit', 'remaining_decisions':supervisor.autonomy}),flush=True)
        store.db.close()
        return 75
    state = store.snapshot(goal)
    records = [dict(row) for row in store.db.execute('SELECT * FROM verifications')]
    for record in records: record['output'] = Path(record['artifact']).read_text()
    events = [dict(row) | {'detail':json.loads(row['detail'])} for row in store.db.execute(
        "SELECT kind,detail FROM activity WHERE kind IN ('process_restart_verified','execution_capabilities','injected_completion_loss','implementation_reconciled')")]
    report = metadata | {'goal_status':state['goal']['status'], 'implementations':state['implementations'],
        'research':store.research_history(goal), 'decisions':state['decisions'],
        'verifications':records, 'audits':[dict(row) for row in store.db.execute('SELECT * FROM completion_audits')],
        'usage':measured_usage(store), 'wall_seconds':time.time()-metadata['started'], 'events':events,
        'recovery':recovery, 'errors':errors, 'stopped_for_budget':stopping,
        'worker_handoffs':[json.loads(row[0]) for row in store.db.execute("SELECT body FROM messages WHERE kind='implementation_handoff'")],
        'implementation_attempts':sum(row['attempts'] for row in state['implementations'])}
    for item in state['implementations']:
        if item.get('candidate'):
            report['candidate_patch'] = git(output/'seeded','diff',metadata['seeded_revision'],item['candidate'])
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['goal_status'],'usage':report['usage'],'seconds':report['wall_seconds'],'errors':errors}),flush=True)
    store.db.close()
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--test-python', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fault', choices=['lost_completion'])
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--allow-code-modifications', action='store_true')
    args = parser.parse_args()
    if not args.allow_code_modifications or not os.getenv('OPENAI_API_KEY'):
        parser.error('Explicit isolated-code authorization and OPENAI_API_KEY are required')
    os.environ['OMNITRIX_MODEL']='gpt-5.4'
    os.environ['OMNITRIX_CODEX_MODEL']='gpt-5.3-codex'
    os.environ['OMNITRIX_CODEX_REASONING_EFFORT']='medium'
    sys.exit(asyncio.run(trial(args)))
