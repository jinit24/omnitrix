"""The offline benchmark must exercise the real persisted workflow, without API calls."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/workflow_benchmark.py'
    spec = importlib.util.spec_from_file_location('workflow_benchmark', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


async def test_control_plane_benchmark_preserves_real_evidence_and_compares_equal_outcomes(tmp_path):
    benchmark = module()
    target = tmp_path / 'benchmark'
    result = await benchmark.run_benchmark(target)
    assert result['passed'], result['scenarios']
    assert result['frozen_code_unchanged']
    assert result['frozen']['provider_calls'] == result['frozen']['external_worker_calls'] == 0
    assert len(result['frozen']['files_sha256']) > 10
    scenarios = {row['name']: row['measurements'] for row in result['scenarios']}
    arms = scenarios['approved_batch_and_dependency_repair']
    assert arms['batch']['manager_strategy_calls'] == 1
    assert arms['per_task_scripted_baseline']['manager_strategy_calls'] == 3
    assert arms['batch']['supported_bounded_contracts'] == arms['per_task_scripted_baseline']['supported_bounded_contracts'] == 3
    assert arms['batch']['same_attempt_scope_grants'] == arms['batch']['attempts'] == 3
    recovery = scenarios['read_only_restart_reconciliation']
    assert recovery['requests_before'] == recovery['requests_after'] == 1
    assert recovery['committed_usd_before'] == recovery['committed_usd_after'] > 0
    # Evidence survives independently of the process that exercised it.
    databases = list(target.rglob('state.sqlite'))
    assert len(databases) == 8
    for path in databases:
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        finally:
            connection.close()
    stored = json.loads((target / 'result.json').read_text())
    assert stored == result
    assert 'not measured' in ' '.join(stored['limitations'])


async def test_failure_is_durable_and_output_cannot_be_replayed_silently(tmp_path, monkeypatch):
    benchmark = module()

    async def broken(_):
        raise AssertionError('Injected invariant failure')

    monkeypatch.setattr(benchmark, 'SCENARIOS', [('fault-injected', broken)])
    target = tmp_path / 'failure'
    report = await benchmark.run_benchmark(target)
    assert not report['passed']
    assert report['scenarios'][0]['error'] == 'AssertionError: Injected invariant failure'
    saved = (target / 'result.json').read_bytes()
    with pytest.raises(FileExistsError):
        await benchmark.run_benchmark(target)
    assert (target / 'result.json').read_bytes() == saved
