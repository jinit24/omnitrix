"""The public-repository harness must preserve evidence and prevent write paths."""
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from scripts import public_research as harness
from omnitrix.budget import Budget
from omnitrix.control import Control
from omnitrix.models import Decision
from omnitrix.research_models import ResearchPlan, DiscoveryReport, MapEntry, Opportunity, ResearchReport, Finding, Evaluation, RubricAssessment
from omnitrix.store import Store


def repository(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://example.test/kubernetes.git'], check=True)
    area = repo / 'staging/src/k8s.io/client-go/util/workqueue'
    area.mkdir(parents=True)
    (area / 'queue.go').write_text('package workqueue\n' + ''.join(f'// line {i}: café\n' for i in range(2500)))
    (area / 'queue_test.go').write_text('package workqueue\n// Existing contracts must be investigated\n')
    (area / 'binary.bin').write_bytes(b'\x00\xff')
    (repo / 'README.md').write_text('Public repository fixture\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'fixture'], check=True, capture_output=True)
    return repo


def decision(action, plan=None):
    return Decision(action=action, rationale='Test decision', instructions='Bounded research', task_id=None, priority=1, research_plan=plan)


def test_snapshot_is_exact_bounded_and_traceable(tmp_path):
    repo = repository(tmp_path)
    directory = tmp_path / 'experiment'
    metadata = harness.prepare('kubernetes', repo, directory)
    snapshot = json.loads((directory / 'source-snapshot.json').read_text())
    assert snapshot['commit'] == harness.git(repo, 'rev-parse', 'HEAD')
    assert any(r['path'] == 'README.md' and not r['included'] for r in snapshot['inventory'])
    binary = next(r for r in snapshot['inventory'] if r['path'].endswith('binary.bin'))
    assert not binary['included']
    assert len([c for c in snapshot['chunks'] if c['original_path'].endswith('queue.go')]) == 3
    grouped = {}
    for chunk in snapshot['chunks']:
        raw = (directory / 'workspace' / chunk['locator']).read_bytes()
        assert len(raw) <= harness.MAX_SOURCE_BYTES
        raw.decode('utf-8')
        assert harness.digest(raw) == chunk['sha256']
        grouped.setdefault(chunk['original_path'], []).append((chunk['original_start_line'], raw))
    for original, chunks in grouped.items():
        assert b''.join(raw for _, raw in sorted(chunks)) == (repo / original).read_bytes()
    report = harness.export(directory)
    assert report['source_integrity']['snapshot_unchanged']
    assert report['source_integrity']['public_checkout_unchanged']
    assert report['database']['integrity'] == ['ok']
    assert report['database']['reopen_ok']
    assert report['budget']['limit_usd'] == 25
    assert report['budget']['requests'] == 0
    assert metadata['model'] == 'gpt-6.1-sol'
    assert 'dollar_requests' in json.loads((directory / 'export/tables.json').read_text())
    with pytest.raises(ValueError, match='already exists'):
        harness.prepare('kubernetes', repo, directory)


def test_split_source_keeps_utf8_and_newline_offsets():
    original = ('é\n' * 20).encode()
    chunks = list(harness.split_source(original, max_bytes=10, max_lines=4))
    assert b''.join(c[2] for c in chunks) == original
    assert chunks[1][0] == chunks[0][1] + 1
    assert all(len(c[2]) <= 10 for c in chunks)
    with pytest.raises(ValueError, match='single source line'):
        list(harness.split_source(b'a' * 11, max_bytes=10))


@pytest.mark.parametrize('action', ['delegate', 'follow_up', 'implement', 'audit'])
async def test_read_only_guard_rejects_every_execution_path(tmp_path, action):
    repo = repository(tmp_path)
    directory = tmp_path / 'experiment'
    metadata = harness.prepare('kubernetes', repo, directory)
    store = Store(directory / 'state.db')
    supervisor = await harness.ReadOnlySupervisor(store, metadata['goal_id'], runtime=harness.DisabledRuntime()).open()
    try:
        with pytest.raises(ValueError, match='prohibited'):
            await supervisor.dispatch(decision(action))
        with pytest.raises(ValueError, match='at least two'):
            await supervisor.dispatch(decision('complete'))
        assert store.db.execute('SELECT count(*) FROM implementations').fetchone()[0] == 0
    finally:
        await supervisor.close()
        store.db.close()


async def test_live_harness_restarts_preserve_records_sessions_and_budget(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    directory = tmp_path / 'experiment'
    metadata = harness.prepare('kubernetes', repo, directory)
    source = json.loads((directory / 'source-snapshot.json').read_text())['chunks'][0]['locator']

    class Brain:
        def __init__(self, model, store):
            self.model, self.store = model, store

        async def decide(self, state):
            session = Control(self.store).session(state['goal']['id'])
            await session.add_items([{'role': 'user', 'content': 'persistent manager test'}])
            session.close()
            count = len(state['decisions'])
            plan = ResearchPlan(scope=[source], question='Which contract warrants deeper investigation?',
                                hypothesis=f'Contract {count} has an observable consistency property' if count else None,
                                falsification='Existing source contradicts the consistency property' if count else None,
                                decision='Decide whether this is worth an executable check',
                                expected_value='Clarify an important contract at low cost', stop_condition='Source contract established')
            return decision('discover' if count == 0 else 'research', plan)

    class Agents:
        def __init__(self, model):
            pass

        async def run(self, role, context, tools):
            evidence = tools.read(source)
            tools.checkpoint([evidence['id']], 'Only static evidence is available', 'Report the bounded observation')
            if role == 'discovery':
                return DiscoveryReport(system_map=[MapEntry(name='queue', kind='service', description='Source fixture', depends_on=[], evidence_ids=[evidence['id']], assumption=False)],
                    opportunities=[Opportunity(title='Inspect contract', hypothesis='The contract has a consistency property', falsification='Source contradicts it', scope=[source], impact=3, confidence=2, risk=1, effort=1, evidence_ids=[evidence['id']], rationale='Important bounded uncertainty')], gaps=['Static fixture only'])
            if role == 'evaluation':
                return Evaluation(verdict='supported', evidence_ids=[evidence['id']], reason='Bounded source assertion', feedback='Execute independent validation later', rubric_assessments=[RubricAssessment(criterion=criterion, verdict='met', reason='Fixture source is retained with scope and checkpoint', evidence_ids=[evidence['id']]) for criterion in context['research_context']['rubric']])
            return ResearchReport(findings=[Finding(statement='The source declares the package', classification='claim', evidence_ids=[evidence['id']])], supporting_evidence=[evidence['id']], contradictory_evidence=[], uncertainties=['No execution'], recommendation='Check an executable contract later', blocked=False)

    monkeypatch.setattr(harness, 'AgentBrain', Brain)
    monkeypatch.setattr(harness, 'ResearchAgents', Agents)
    report = await harness.run(directory, steps=3, seconds=30, restart_every=1)
    assert report['counts']['decisions'] == 3
    assert report['counts']['research_jobs'] == 3
    assert report['counts']['research_runs'] == 3
    assert report['counts']['knowledge'] >= 3
    assert report['counts']['checkpoints'] >= 3
    assert report['budget']['requests'] == 0
    assert len(report['experiment']['restarts']) == 3
    assert all(c['persisted_counts_unchanged'] for c in report['experiment']['restarts'])
    assert report['manager_session']['reopen_ok']
    assert report['manager_session']['table_counts']['agent_messages'] == 3
    saved = json.loads((directory / 'export/tables.json').read_text())
    assert len(saved['research_runs']) == 3
    store = Store(directory / 'state.db')
    before = Budget(store, metadata['goal_id']).summary()
    supervisor = await harness.ReadOnlySupervisor(store, metadata['goal_id'], runtime=harness.DisabledRuntime()).open()
    assert before == Budget(store, metadata['goal_id']).summary()
    await supervisor.close()
    store.db.close()


async def test_exception_and_tampering_are_exported_not_hidden(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    directory = tmp_path / 'experiment'
    harness.prepare('kubernetes', repo, directory)

    class BrokenBrain:
        def __init__(self, **kwargs):
            pass
        async def decide(self, state):
            raise RuntimeError('Simulated provider failure')

    monkeypatch.setattr(harness, 'AgentBrain', BrokenBrain)
    result = await harness.run(directory, steps=1, seconds=5)
    assert result['experiment']['state'] == 'failed'
    assert 'Simulated provider failure' in result['experiment']['last_error']
    assert 'run_exception' in (directory / 'events.jsonl').read_text()
    snapshot = json.loads((directory / 'source-snapshot.json').read_text())
    (directory / 'workspace' / snapshot['chunks'][0]['locator']).write_text('tampered')
    result = harness.export(directory)
    assert not result['source_integrity']['snapshot_unchanged']
    assert result['source_integrity']['public_checkout_unchanged']


def test_whole_repository_upgrade_keeps_budget_and_indexes_every_tracked_path(tmp_path):
    repo = repository(tmp_path)
    directory = tmp_path / 'experiment'
    metadata = harness.prepare('kubernetes', repo, directory)
    store = Store(directory / 'state.db')
    budget = Budget(store, metadata['goal_id'])
    budget.reserve(json.dumps({'model': metadata['model'], 'service_tier': 'default',
                               'max_output_tokens': 8, 'input': 'No network request is issued by this fixture'}).encode(), 'manager')
    before = budget.summary()
    store.db.close()
    result = harness.upgrade_full_repository(directory, repo)
    assert result['experiment']['goal_id'] == metadata['goal_id']
    assert result['experiment']['scope'] == 'whole_repository'
    assert result['budget'] == before
    assert result['source_integrity']['public_checkout_unchanged']
    eligibility = json.loads((directory / 'completion-check.json').read_text())
    assert eligibility['top_level_areas'] == 2
    assert eligibility['currently_eligible_top_level'] == 0
    assert not eligibility['complete_coverage_floor_met']
    assert len(eligibility['gaps']) == 2
    snapshot = json.loads((directory / 'source-snapshot.json').read_text())
    tracked = set(harness.git(repo, 'ls-files', '-z').split('\0')) - {''}
    assert {row['path'] for row in snapshot['inventory']} == tracked
    readme = next(row for row in snapshot['inventory'] if row['path'] == 'README.md')
    assert readme['included']
    assert all(chunk['locator'].startswith('sources/full/') for chunk in snapshot['chunks'])
    assert (directory / 'history/bounded-source-snapshot.json').is_file()
    assert (directory / 'workspace/MANIFEST.json').stat().st_size <= harness.MAX_SOURCE_BYTES
    assert (directory / 'workspace/catalog/full/INDEX.md').is_file()
    store = Store(directory / 'state.db')
    from omnitrix.audit import Audit
    context=Control(store).research_context(metadata['goal_id'])
    assert context['require_checkpoint'] is True and context['require_evidence_plan'] is True
    summary = Audit(store, metadata['goal_id']).summary()
    assert summary['inventory_files'] == len(tracked)
    assert summary['areas_total'] == 2
    assert summary['by_status']['unexamined'] == 2
    assert store.db.execute('SELECT max_runs FROM research_limits').fetchone()[0] == 100
    assert len(store.db.execute('SELECT * FROM dollar_requests').fetchall()) == 1
    store.db.close()
    with pytest.raises(ValueError, match='already has'):
        harness.upgrade_full_repository(directory, repo)


async def test_frontier_triage_and_two_hypotheses_cannot_complete_whole_audit(tmp_path):
    from omnitrix.audit import Audit
    repo=repository(tmp_path);directory=tmp_path/'experiment'
    metadata=harness.prepare('kubernetes',repo,directory)
    store=Store(directory/'state.db');goal=metadata['goal_id']
    Audit(store,goal).initialize([{'path':'staging','files':3,'bytes':100,'kind':'first-party','source_prefixes':['sources']}])
    Audit(store,goal).update('staging','triaged',1,'Important client code','Inspect current queue contracts')
    for i,stage in enumerate(['discovery','research','research']):
        spec=json.dumps({'hypothesis':str(i)})
        store.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?)',(str(i),goal,stage,str(i),spec,'discovered' if stage=='discovery' else 'supported',1,0,0))
        if stage=='research': store.execute("INSERT INTO research_runs(id,job_id,status,evaluation,created) VALUES(?,?,'supported','{}',0)",('run'+str(i),str(i)))
    supervisor=await harness.ReadOnlySupervisor(store,goal,runtime=harness.DisabledRuntime()).open()
    try:
        with pytest.raises(ValueError,match='lack independent source sampling'):
            await supervisor.dispatch(decision('complete'))
        assert supervisor.status()['goal']['status']=='active'
    finally: await supervisor.close()


async def test_read_only_harness_executes_a_saved_batch_without_manager_per_worker(tmp_path,monkeypatch):
    repo=repository(tmp_path);directory=tmp_path/'experiment'
    harness.prepare('kubernetes',repo,directory)
    source=json.loads((directory/'source-snapshot.json').read_text())['chunks'][0]['locator']
    class Brain:
        calls=0
        def __init__(self,model,store): self.model=model
        async def decide(self,state):
            Brain.calls+=1
            if not state['decisions']:
                plans=[{'stage':'discovery','plan':{'scope':[source],
                    'question':f'Which independent contract should be mapped for area {number}?',
                    'decision':'Select the next bounded question','expected_value':'Avoid another manager call per worker',
                    'stop_condition':'A source-grounded contract map is retained'},
                    'priority':2,'instructions':'Inspect this explicit read-only source'} for number in (1,2)]
                return Decision(action='schedule_research',rationale='Persist two bounded questions',instructions='Investigate approved scopes',
                    task_id=None,priority=2,research_batch=plans)
            return decision('pause')
    class Agents:
        calls=0
        def __init__(self,model): pass
        async def run(self,role,context,tools):
            Agents.calls+=1
            assert role=='discovery'
            evidence=tools.read(source)
            tools.checkpoint([evidence['id']],'Runtime validation remains unavailable','Retain the bounded source map')
            return DiscoveryReport(system_map=[MapEntry(name='queue',kind='repository',description='Static source contract',
                depends_on=[],evidence_ids=[evidence['id']],assumption=False)],opportunities=[],gaps=['Only source was inspected'])
    monkeypatch.setattr(harness,'AgentBrain',Brain)
    monkeypatch.setattr(harness,'ResearchAgents',Agents)
    report=await harness.run(directory,steps=4,seconds=30,restart_every=1)
    assert Brain.calls==2 and Agents.calls==2
    assert report['counts']['research_jobs']==2
    assert report['counts']['research_runs']==2
    assert report['budget']['requests']==0
    tables=json.loads((directory/'export/tables.json').read_text())
    assert len(tables['campaign_queue'])==2
    assert {row['state'] for row in tables['campaign_queue']}=={'completed'}
    assert len([row for row in tables['campaign_events'] if row['kind']=='claimed'])==2
