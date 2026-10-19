import json

import pytest

from omnitrix.audit import Audit
from omnitrix.research import Research
from omnitrix.research_models import ResearchPlan
from omnitrix.store import Store
from tests_helpers import FixtureAgents


@pytest.fixture
def state(tmp_path):
    (tmp_path / 'sources/pkg').mkdir(parents=True)
    (tmp_path / 'sources/pkg/queue.md').write_text('Polling interval=30 seconds')
    (tmp_path / 'sources/docs').mkdir()
    (tmp_path / 'sources/docs/guide.md').write_text('Polling interval=30 seconds')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Audit repository', ['Retain uncertainties'], tmp_path)
    audit = Audit(store, goal)
    audit.initialize([
        {'path': 'pkg', 'files': 100, 'bytes': 10000, 'kind': 'directory'},
        {'path': 'docs', 'files': 3, 'bytes': 500, 'kind': 'directory'},
    ])
    return store, goal, audit, tmp_path


async def accepted(store, goal, scope='sources/pkg/queue.md'):
    research = Research(store, FixtureAgents())
    spec = ResearchPlan(scope=[scope], question='What is the polling contract?',
        hypothesis='Polling interval is 30 seconds', falsification='Source specifies another interval')
    job = research.plan(goal, 'research', spec)['job_id']
    outcome = await research.attempt(job)
    assert outcome['status'] == 'supported', outcome['feedback']
    return job


def test_inventory_updates_and_next_questions_survive_restart(state):
    store, goal, audit, _ = state
    audit.update('pkg', 'triaged', 1, 'Request handling exposes user inputs', 'Which parser invariants lack tests?')
    result = audit.update('pkg', 'investigating', 1, 'Trace a bounded request path', 'Does cancellation release work?')
    assert result['revision'] == 3
    fresh = Audit(Store(store.path), goal)
    row = fresh.list(query='cancellation')['areas'][0]
    assert row['path'] == 'pkg' and row['next_question'] == 'Does cancellation release work?'
    assert fresh.summary()['by_status']['investigating'] == 1
    assert store.db.execute('SELECT count(*) FROM audit_area_updates WHERE area=?', ('pkg',)).fetchone()[0] == 3


def test_inventory_is_idempotent_and_metadata_immutable(state):
    _, _, audit, _ = state
    before = audit.summary()
    audit.initialize([{'path': 'pkg', 'files': 100, 'bytes': 10000, 'kind': 'directory'}])
    assert audit.summary() == before
    with pytest.raises(ValueError, match='immutable'):
        audit.initialize([{'path': 'pkg', 'files': 101, 'bytes': 10000, 'kind': 'directory'}])
    assert audit.summary() == before


def test_agenda_pagination_priority_and_literal_search(state):
    _, _, audit, _ = state
    audit.initialize([{'path': f'area-{i:03}', 'files': 1, 'bytes': 1, 'kind': 'directory'} for i in range(45)])
    audit.update('pkg', 'triaged', 1, 'Priority contains literal 50%_token', 'Investigate inputs')
    page = audit.list()
    assert page['total'] == 47 and len(page['areas']) == 20 and page['next_offset'] == 20
    assert page['areas'][0]['path'] == 'pkg'
    pages = page['areas'] + audit.list(offset=20)['areas'] + audit.list(offset=40)['areas']
    assert len({area['path'] for area in pages}) == 47
    assert audit.list(query='50%_')['total'] == 1
    assert audit.list(status='triaged')['total'] == 1


@pytest.mark.parametrize('status', ['reviewed', 'completed', 'audited'])
def test_complete_area_claims_are_not_supported(state, status):
    _, _, audit, _ = state
    with pytest.raises(ValueError, match='sampled'):
        audit.update('pkg', status, 1, 'Read one file', 'Investigate remaining files')


async def test_sampled_requires_same_goal_independent_review_and_area_evidence(state):
    store, goal, audit, tmp_path = state
    with pytest.raises(ValueError, match='independently evaluated'):
        audit.update('pkg', 'sampled', 1, 'Inspected source', 'Explore remaining flows')
    other = store.create_goal('Different goal', ['Review sources'], tmp_path)
    foreign_job = await accepted(store, other)
    with pytest.raises(ValueError, match='belong'):
        audit.update('pkg', 'sampled', 1, 'Inspected source', 'Explore remaining flows', foreign_job)
    wrong_area_job = await accepted(store, goal, 'sources/docs/guide.md')
    with pytest.raises(ValueError, match='source prefixes'):
        audit.update('pkg', 'sampled', 1, 'Inspected guide', 'Explore remaining flows', wrong_area_job)
    job = await accepted(store, goal)
    result = audit.update('pkg', 'sampled', 1, 'Evaluated one polling contract', 'Check cancellation and unexamined queues', job)
    assert result['status'] == 'sampled'
    assert audit.summary()['by_status']['sampled'] == 1
    assert audit.summary()['inventory_files'] == 103
    assert 'does not establish complete' in audit.summary()['coverage_note']


async def test_stale_or_missing_evaluation_cannot_establish_sample(state):
    store, goal, audit, tmp_path = state
    job = await accepted(store, goal)
    store.execute('UPDATE research_runs SET evaluation=NULL WHERE job_id=?', (job,))
    with pytest.raises(ValueError, match='independent evaluation'):
        audit.update('pkg', 'sampled', 1, 'Source read', 'Review more paths', job)
    store.execute('UPDATE research_runs SET evaluation=? WHERE job_id=?',
        (json.dumps({'evidence_ids': [], 'verdict': 'supported'}), job))
    (tmp_path / 'sources/pkg/queue.md').write_text('Polling interval=10 seconds')
    with pytest.raises(ValueError, match='current cited evidence'):
        audit.update('pkg', 'sampled', 1, 'Old source read', 'Review current paths', job)


def test_invalid_inventory_and_updates_are_rejected(state):
    _, _, audit, _ = state
    with pytest.raises(ValueError, match='inside'):
        audit.initialize([{'path': '../outside', 'files': 1, 'bytes': 1, 'kind': 'file'}])
    with pytest.raises(ValueError, match='priority'):
        audit.update('pkg', 'triaged', 0, 'Important', 'What next?')
    with pytest.raises(ValueError, match='next question'):
        audit.update('pkg', 'deferred', 3, 'Lower expected value', '')
    with pytest.raises(ValueError, match='Unknown audit area'):
        audit.update('unknown', 'triaged', 3, 'Investigate', 'What next?')


def test_expand_uses_actual_source_stems_and_does_not_double_count(state):
    store, goal, audit, tmp_path = state
    directory = tmp_path / 'sources/pkg/components'
    directory.mkdir()
    (directory / 'controller.go.part-0001').write_text('first')
    (directory / 'controller.go.part-0002').write_text('second')
    (directory / 'controller_test.go.part-0001').write_text('test')
    (directory / 'nested').mkdir()
    (directory / 'nested/plain.go').write_text('plain')
    (directory / '.git').mkdir()
    (directory / '.git/config').write_text('excluded')
    (directory / 'linked').symlink_to(tmp_path / 'sources/docs', target_is_directory=True)
    before = audit.summary()
    result = audit.expand('pkg', 'pkg/controllers', 'sources/pkg/components')
    assert result['files'] == 3 and result['source_chunks'] == 4 and result['bytes'] == 20
    assert result['skipped_symlinks'] == 1 and result['skipped_excluded'] == 1
    summary = audit.summary()
    assert summary['inventory_files'] == before['inventory_files']
    assert summary['inventory_bytes'] == before['inventory_bytes']
    assert summary['subareas_total'] == 1 and summary['areas_total'] == before['areas_total'] + 1
    area = audit.list(query='pkg/controllers')['areas'][0]
    assert area['status'] == 'unexamined' and area['kind'] == 'subarea'
    assert area['source_prefixes'] == ['sources/pkg/components']
    assert 'Metadata is navigation' in area['rationale']
    again = audit.expand('pkg', 'pkg/controllers', 'sources/pkg/components')
    assert again['reused']
    assert audit.list(query='pkg/controllers')['areas'][0]['revision'] == area['revision']
    assert Audit(Store(store.path), goal).summary() == summary


def test_expand_rejects_outside_ancestors_excluded_and_symlinks(state):
    _, _, audit, tmp_path = state
    for directory in ('sources/docs', 'sources', '../outside', 'sources/pkg'):
        with pytest.raises(ValueError):
            audit.expand('pkg', 'pkg/child', directory)
    (tmp_path / 'sources/pkg/link').symlink_to(tmp_path / 'sources/docs', target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        audit.expand('pkg', 'pkg/child', 'sources/pkg/link')
    (tmp_path / 'sources/pkg/.git').mkdir()
    with pytest.raises(ValueError, match='Excluded'):
        audit.expand('pkg', 'pkg/child', 'sources/pkg/.git')
    with pytest.raises(ValueError, match='logical descendant'):
        audit.expand('pkg', 'docs/child', 'sources/pkg/components')


async def test_expanded_subarea_requires_its_own_evidence(state):
    store, goal, audit, tmp_path = state
    directory = tmp_path / 'sources/pkg/components'
    directory.mkdir()
    (directory / 'worker.md').write_text('Polling interval=30 seconds')
    audit.expand('pkg', 'pkg/workers', 'sources/pkg/components')
    parent_job = await accepted(store, goal)
    with pytest.raises(ValueError, match='source prefixes'):
        audit.update('pkg/workers', 'sampled', 1, 'Parent contract reviewed', 'Inspect worker failures', parent_job)
    own_job = await accepted(store, goal, 'sources/pkg/components/worker.md')
    assert audit.update('pkg/workers', 'sampled', 1, 'Worker contract evaluated',
        'Inspect untested cancellation paths', own_job)['status'] == 'sampled'


async def test_core_supervisor_cannot_complete_an_unsampled_frontier(state):
    from omnitrix.supervisor import Supervisor
    from omnitrix.models import Decision
    store,goal,audit,_=state
    audit.update('pkg','triaged',1,'Important request path','Investigate parsers')
    audit.update('docs','deferred',5,'Lower near-term value','Review documented contracts later')
    assert len(audit.completion_gaps())==2
    supervisor=await Supervisor(store,goal).open()
    try:
        with pytest.raises(ValueError,match='Audit remains incomplete'):
            await supervisor.dispatch(Decision(action='complete',rationale='Two findings are enough',instructions='Done',task_id=None,priority=1))
        assert supervisor.status()['goal']['status']=='active'
    finally: await supervisor.close()


async def sampled_frontier(state):
    store, goal, audit, _ = state
    for area, source in (('pkg', 'sources/pkg/queue.md'), ('docs', 'sources/docs/guide.md')):
        job = await accepted(store, goal, source)
        audit.update(area, 'sampled', 2, 'Independently evaluated bounded contract',
            'Retain unexamined behavior as future work', job)
    assert audit.completion_gaps() == []
    return list(store.db.execute('SELECT * FROM audit_area_updates ORDER BY area,revision'))


async def test_completion_rechecks_changed_sample_without_rewriting_history(state):
    store, _, audit, path = state
    history = await sampled_frontier(state)
    (path / 'sources/pkg/queue.md').write_text('Polling interval=10 seconds')
    gaps = audit.completion_gaps()
    assert len(gaps) == 1 and gaps[0]['path'] == 'pkg' and gaps[0]['status'] == 'sampled'
    assert 'stale' in gaps[0]['reason']
    assert audit.summary()['by_status']['sampled'] == 2
    assert list(store.db.execute('SELECT * FROM audit_area_updates ORDER BY area,revision')) == history


async def test_completion_rechecks_context_invalidated_samples_without_rewriting_history(state):
    from omnitrix.control import Control
    store, goal, audit, _ = state
    history = await sampled_frontier(state)
    control = Control(store)
    proposal = control.propose(goal, 'research_context',
        {'brief': 'Require stronger runtime evidence', 'rubric': ['Address actual caller behavior']})
    control.accept(goal, proposal)
    gaps = audit.completion_gaps()
    assert {gap['path'] for gap in gaps} == {'pkg', 'docs'}
    assert all(gap['status'] == 'sampled' and 'status=stale' in gap['reason'] for gap in gaps)
    assert list(store.db.execute('SELECT * FROM audit_area_updates ORDER BY area,revision')) == history


@pytest.mark.parametrize('kind,body', [
    ('policy', {'instructions': 'Require explicit observed caller contracts'}),
    ('requirements', {'constraint': 'Sample every top-level area under the current completion contract',
                      'add_requirements': ['Retain all unexamined top-level gaps']}),
])
async def test_completion_fences_supported_jobs_from_older_policy_or_goal_contract(state, kind, body):
    from omnitrix.control import Control
    store, goal, audit, _ = state
    history = await sampled_frontier(state)
    control = Control(store)
    proposal = control.propose(goal, kind, body)
    control.accept(goal, proposal)
    assert {row[0] for row in store.db.execute('SELECT status FROM research_jobs')} == {'supported'}
    gaps = audit.completion_gaps()
    assert len(gaps) == 2
    assert all('revisions differ' in gap['reason'] for gap in gaps)
    assert audit.summary()['by_status']['sampled'] == 2
    assert list(store.db.execute('SELECT * FROM audit_area_updates ORDER BY area,revision')) == history


async def test_one_current_excerpt_cannot_mask_another_changed_cited_source(state):
    store, goal, audit, path = state

    class TwoSources(FixtureAgents):
        async def run(self, role, context, tools):
            result = await super().run(role, context, tools)
            if role == 'research':
                other = tools.read('sources/pkg/queue.md')
                result.findings[0].evidence_ids.append(other['id'])
                result.supporting_evidence.append(other['id'])
            return result

    service = Research(store, TwoSources())
    spec = ResearchPlan(scope=['sources/pkg/queue.md', 'sources/docs/guide.md'],
        question='What contract is shared across these two sources?', hypothesis='Polling interval is 30 seconds',
        falsification='Either source specifies a different interval')
    job = service.plan(goal, 'research', spec)['job_id']
    assert (await service.attempt(job))['status'] == 'supported'
    audit.update('pkg', 'sampled', 1, 'Compared implementation and contract', 'Inspect remaining paths', job)
    assert not any(gap['path'] == 'pkg' for gap in audit.completion_gaps())
    (path / 'sources/docs/guide.md').write_text('Polling interval=10 seconds')
    assert any(gap['path'] == 'pkg' and 'stale' in gap['reason'] for gap in audit.completion_gaps())


async def test_core_supervisor_rejects_completion_after_previously_valid_sample_changes(state):
    from omnitrix.models import Decision
    from omnitrix.supervisor import Supervisor
    store, goal, _, path = state
    await sampled_frontier(state)
    (path / 'sources/pkg/queue.md').write_text('Polling interval=10 seconds')
    supervisor = await Supervisor(store, goal).open()
    try:
        with pytest.raises(ValueError, match='Audit remains incomplete'):
            await supervisor.dispatch(Decision(action='complete', rationale='All recorded states are sampled',
                instructions='Complete audit', task_id=None, priority=1))
        assert supervisor.status()['goal']['status'] == 'active'
    finally:
        await supervisor.close()


async def test_record_result_attributes_only_declared_primary_area_and_retains_gaps(state):
    store,goal,audit,_=state
    class PrimaryEvidence(FixtureAgents):
        async def run(self,role,context,tools):
            result=await super().run(role,context,tools)
            if role=='research':
                primary=tools.read('sources/pkg/queue.md')
                result.findings[0].evidence_ids.append(primary['id'])
                result.supporting_evidence.append(primary['id'])
            return result
    service=Research(store,PrimaryEvidence())
    spec=ResearchPlan(primary_area='pkg',scope=['sources/pkg/queue.md','sources/docs/guide.md'],
        question='Which area owns this bounded contract?',hypothesis='Polling interval is 30 seconds',
        falsification='The source states a different interval')
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='supported'
    result=audit.record_result(job)
    latest={row['path']:row for row in audit.list()['areas']}
    assert latest['pkg']['status']=='sampled'
    assert latest['docs']['status']=='unexamined'
    ledger=store.db.execute('SELECT * FROM audit_sample_results WHERE job_id=?',(job,)).fetchone()
    assert 'Production measurement unavailable' in json.loads(ledger['remaining_gaps'])
    before=store.db.execute('SELECT count(*) FROM audit_area_updates').fetchone()[0]
    assert audit.record_result(job)['recorded'] is False
    assert store.db.execute('SELECT count(*) FROM audit_area_updates').fetchone()[0]==before
    assert result['recorded'] or 'already recorded' in result['reason']
    with pytest.raises(ValueError,match='primary_area'):
        audit.update('docs','sampled',2,'The scope contains documentation','Read more documentation',job)


async def test_unaccepted_or_changed_source_results_do_not_automatically_sample(state):
    store,goal,audit,path=state
    service=Research(store,FixtureAgents(verdict='insufficient'))
    spec=ResearchPlan(primary_area='pkg',scope=['sources/pkg/queue.md'],question='Does this contract warrant acceptance?',
        hypothesis='Polling interval is 30 seconds',falsification='Source states another interval')
    job=service.plan(goal,'research',spec)['job_id']
    assert (await service.attempt(job))['status']=='insufficient'
    assert audit.record_result(job)['recorded'] is False
    assert audit.summary()['by_status']['sampled']==0
    other=await accepted(store,goal,'sources/docs/guide.md')
    assert audit.record_result(other)['recorded'] is False  # No attribution inferred for legacy plans.
    accepted_spec=spec.model_copy(update={'question':'Does a new bounded question resolve this contract?','hypothesis':'Polling interval remains exactly 30 seconds'})
    job=Research(store,FixtureAgents()).plan(goal,'research',accepted_spec)['job_id']
    assert (await Research(store,FixtureAgents()).attempt(job))['status']=='supported'
    (path/'sources/pkg/queue.md').write_text('Polling interval changed')
    assert audit.record_result(job)['recorded'] is False
    assert any(gap['path']=='pkg' for gap in audit.completion_gaps())


async def test_accepted_append_only_review_records_coverage_without_rewriting_original(state):
    from omnitrix.control import Control
    store,goal,audit,_=state
    service=Research(store,FixtureAgents(verdict='insufficient'))
    spec=ResearchPlan(primary_area='pkg',scope=['sources/pkg/queue.md'],question='Which supported evidence survives the first failed gate?',
        hypothesis='Polling interval is 30 seconds',falsification='Source states another interval')
    job=service.plan(goal,'research',spec)['job_id'];await service.attempt(job)
    run=store.db.execute('SELECT * FROM research_runs WHERE job_id=?',(job,)).fetchone()
    original=run['evaluation'];evaluation=json.loads(original);evaluation['verdict']='supported'
    store.execute('INSERT INTO research_reviews(id,job_id,run_id,feedback,context,revisions,status,evaluation,created,finished) VALUES(?,?,?,?,?,?,?,?,?,?)',
        ('review',job,run['id'],'Reassess retained report','{}',json.dumps(Control(store).current(goal)),'supported',json.dumps(evaluation),1,2))
    store.execute("UPDATE research_jobs SET status='supported' WHERE id=?",(job,))
    assert audit.record_result(job)['recorded'] is True
    assert store.db.execute('SELECT evaluation FROM research_runs WHERE id=?',(run['id'],)).fetchone()[0]==original
    assert store.db.execute('SELECT review_id FROM audit_sample_results WHERE job_id=?',(job,)).fetchone()[0]=='review'
