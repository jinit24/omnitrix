"""Adversarial research regressions, separate from mocked model-quality claims."""
import json

import pytest

from omnitrix.knowledge import ResearchTools
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store


def ledger(tmp_path, scope, calls=8):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Find the highest impact system inefficiency', ['Compare evidence'], tmp_path)
    service = Research(store)
    job = service.plan(goal, 'discovery', ResearchPlan(scope=scope, question='Which subsystem warrants investigation?'))
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('run',?,'running',0)", (job['job_id'],))
    return store, goal, ResearchTools(store, 'run', str(tmp_path), scope, calls)


def test_inventory_pages_past_distractors_and_excludes_runtime(tmp_path):
    from omnitrix.resources import list_resources
    for i in range(85):
        (tmp_path / f'a{i:03}.txt').write_text('noise')
    (tmp_path / 'z_metrics.csv').write_text('operation,ms\nscan,900\n')
    (tmp_path / 'node_modules').mkdir()
    pages = [list_resources(tmp_path, offset=i) for i in (0, 40, 80)]
    entries = [e for p in pages for e in p['entries']]
    assert len(entries) == 86
    assert pages[0]['next_offset'] == 40 and pages[-1]['next_offset'] is None
    assert entries[-1]['path'] == 'z_metrics.csv'
    assert entries[-1]['kind'] == 'metrics'


def test_inventory_refuses_escape_and_does_not_follow_directory_links(tmp_path):
    from omnitrix.resources import list_resources
    (tmp_path / 'outside').symlink_to('/tmp', target_is_directory=True)
    assert not list_resources(tmp_path)['entries']
    with pytest.raises(ValueError):
        list_resources(tmp_path, '../')


def test_large_log_tail_is_searchable_and_citable(tmp_path):
    (tmp_path / 'worker.log').write_text('INFO healthy idle poll\n' * 9000 + 'ERROR batch fallback has 9000 serial queries\n')
    store, _, tools = ledger(tmp_path, ['worker.log'])
    cursor = 1
    observations = []
    while cursor:
        result = tools.search_source('worker.log', 'serial queries', start_line=cursor)
        observations.append(result)
        cursor = result['next_line']
    assert len(observations) > 1
    assert any('serial queries' in r['excerpt'] for r in observations)
    assert observations[-1]['id'] in {s['id'] for s in tools.evidence()}
    assert store.db.execute("SELECT count(*) FROM research_queries WHERE operation='search_source'").fetchone()[0] == len(observations)


def test_csv_aggregation_compares_total_cost_instead_of_loudest_warning(tmp_path):
    (tmp_path / 'latency.csv').write_text('operation,ms\n' + 'cache,1\n' * 6000 + 'database,90000\n')
    _, _, tools = ledger(tmp_path, ['latency.csv'])
    first = tools.summarize_csv('latency.csv', 'ms', 'operation')
    assert first['summary']['partial'] and first['summary']['next_row']
    last = tools.summarize_csv('latency.csv', 'ms', 'operation', start_row=first['summary']['next_row'])
    assert last['summary']['groups']['database']['sum'] == 90000
    assert not last['summary']['partial']
    assert json.loads(last['excerpt'])['measurement_window'] == 'Not inferred; inspect timestamp columns and source context'


def test_read_reports_excerpt_clipping_without_silently_omitting_lines(tmp_path):
    (tmp_path / 'long.log').write_text('x' * 9000 + '\nimportant tail\n')
    _, _, tools = ledger(tmp_path, ['long.log'])
    result = tools.read('long.log', line_count=2)
    assert result['truncated'] and result['next_line'] == 1
    assert 'important tail' in tools.read('long.log', start_line=2)['excerpt']


def test_progress_reveals_unread_metrics_and_unexplored_alternatives(tmp_path):
    from omnitrix.knowledge import Knowledge
    (tmp_path / 'code.py').write_text('def load(): pass\n')
    (tmp_path / 'metrics.csv').write_text('name,ms\ndb,999\n')
    store, goal, tools = ledger(tmp_path, ['code.py', 'metrics.csv'])
    tools.read('code.py')
    progress = store.snapshot(goal)['research_progress']
    assert progress['observed_by_kind']['code'] == ['code.py']
    assert progress['unobserved_planned_sources'] == ['metrics.csv']
    assert any('another independent' in a for a in progress['advice'])
    tools.summarize_csv('metrics.csv', 'ms', 'name')
    assert not Knowledge(store).progress(goal)['unobserved_planned_sources']


def test_search_and_summary_reject_out_of_scope_and_charge_budgets(tmp_path):
    (tmp_path / 'allowed.md').write_text('allowed')
    (tmp_path / 'other.csv').write_text('name,value\na,12\n')
    _, _, tools = ledger(tmp_path, ['allowed.md'], calls=2)
    with pytest.raises(ValueError, match='scope'):
        tools.search_source('other.csv', '12')
    with pytest.raises(ValueError, match='scope'):
        tools.summarize_csv('other.csv', 'value')
    with pytest.raises(RuntimeError, match='budget'):
        tools.read('allowed.md')


def test_summary_reports_invalid_values_and_freshness(tmp_path):
    from omnitrix.knowledge import Knowledge
    path = tmp_path / 'metrics.csv'
    path.write_text('name,value\na,2\na,6\na,NaN\na,unknown\n')
    store, _, tools = ledger(tmp_path, ['metrics.csv'])
    result = tools.summarize_csv('metrics.csv', 'value', 'name')
    assert result['summary']['invalid_values'] == 2
    assert result['summary']['groups']['a'] == {'count': 2, 'sum': 8, 'min': 2, 'max': 6, 'mean': 4}
    path.write_text('name,value\na,1\n')
    assert Knowledge(store).source(result['id'])['stale']


def test_search_continuation_does_not_skip_matches_or_loop_on_long_line(tmp_path):
    (tmp_path / 'worker.log').write_text(('x' * 8500 + ' NEEDLE\n') + 'needle\n' * 21)
    _, _, tools = ledger(tmp_path, ['worker.log'])
    first = tools.search_source('worker.log', 'needle')
    assert first['next_line'] == 21
    assert len(first['matches']) == 20
    assert 'NEEDLE' in first['matches'][0]
    second = tools.search_source('worker.log', 'needle', first['next_line'])
    assert len(second['matches']) == 2
    assert second['next_line'] is None


def test_csv_group_limit_is_paginated_without_dropping_rows(tmp_path):
    (tmp_path / 'metrics.csv').write_text('name,value\n' + ''.join(f'group{i},1\n' for i in range(25)))
    _, _, tools = ledger(tmp_path, ['metrics.csv'])
    first = tools.summarize_csv('metrics.csv', 'value', 'name')
    second = tools.summarize_csv('metrics.csv', 'value', 'name', first['summary']['next_row'])
    assert first['summary']['scanned_rows'] + second['summary']['scanned_rows'] == 25
    assert len(first['summary']['groups']) == 20
    assert not second['summary']['partial']


def test_full_summary_follows_pages_within_budget_and_combines_weighted_means(tmp_path):
    (tmp_path / 'metrics.csv').write_text('name,value\n' + 'cache,1\n' * 6000 + 'db,90000\n')
    _, _, tools = ledger(tmp_path, ['metrics.csv'], calls=2)
    result = tools.summarize_csv('metrics.csv', 'value', 'name', complete=True)
    summary = result['summary']
    assert summary['pages_aggregated'] == 2 and summary['scanned_rows'] == 6001
    assert summary['groups']['cache']['mean'] == 1
    assert summary['groups']['db']['sum'] == 90000
    assert not summary['partial'] and tools.remaining == 0


def test_full_summary_keeps_incompleteness_when_budget_runs_out(tmp_path):
    (tmp_path / 'metrics.csv').write_text('name,value\n' + 'cache,1\n' * 6000 + 'db,90000\n')
    _, _, tools = ledger(tmp_path, ['metrics.csv'], calls=1)
    result = tools.summarize_csv('metrics.csv', 'value', 'name', complete=True)
    assert result['summary']['partial'] and result['summary']['next_row'] == 5001
    assert 'db' not in result['summary']['groups']


async def test_new_research_tools_work_through_actual_sdk_dispatch(tmp_path, monkeypatch):
    from agents.tool_context import ToolContext
    from omnitrix.research import ResearchAgents
    (tmp_path / 'metrics.csv').write_text('name,value\ndb,500\n')
    _, _, tools = ledger(tmp_path, ['metrics.csv'])
    async def fake_run(agent, prompt, **kwargs):
        assert agent.model_settings.tool_choice == 'summarize_csv'
        by_name = {t.name: t for t in agent.tools}
        for name, arguments in [('search_source', {'locator': 'metrics.csv', 'query': 'db'}),
                                ('summarize_csv', {'locator': 'metrics.csv', 'value_column': 'value', 'group_by': 'name'})]:
            payload = json.dumps(arguments)
            context = ToolContext(context=None, tool_name=name, tool_call_id=name, tool_arguments=payload)
            result = json.loads(await by_name[name].on_invoke_tool(context, payload))
            assert result['id']
        return type('Result', (), {'final_output': {}})()
    monkeypatch.setattr('omnitrix.research.Runner.run', fake_run)
    await ResearchAgents().run('discovery', {}, tools)


async def test_discovery_cannot_complete_without_observing_any_evidence(tmp_path):
    (tmp_path / 'source.md').write_text('available facts')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Discover evidence', ['Inspect sources'], tmp_path)
    class NoReads:
        async def run(self, *args):
            return {'system_map': [], 'opportunities': [], 'gaps': ['No evidence inspected']}
    service = Research(store, NoReads())
    job = service.plan(goal, 'discovery', ResearchPlan(scope=['source.md'], question='Which resource has useful facts?'))
    result = await service.attempt(job['job_id'])
    assert result['status'] == 'blocked'
    assert 'no current evidence' in result['feedback']
