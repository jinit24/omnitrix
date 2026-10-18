"""Acceptance must bind review evidence and goal ownership correctly."""
from omnitrix.research import Research
from omnitrix.research_models import Evaluation, Finding, ResearchPlan, ResearchReport
from omnitrix.store import Store


def test_supported_verdict_must_cover_every_claim_not_an_unrelated_citation():
    report = ResearchReport(findings=[Finding(statement='Database is the bottleneck', classification='claim', evidence_ids=['db'])],
        supporting_evidence=['db'], contradictory_evidence=[], uncertainties=[], recommendation='Batch reads', blocked=False)
    evaluation = Evaluation(verdict='supported', evidence_ids=['cache'], reason='Cache warning observed', feedback='Proceed')
    result = Research.gate(report, evaluation, [{'id': 'db', 'stale': False}, {'id': 'cache', 'stale': False}])
    assert result.verdict == 'insufficient'


def test_duplicate_investigation_does_not_return_another_goals_job(tmp_path):
    (tmp_path / 'system.md').write_text('system')
    store = Store(tmp_path / 'state.db')
    first = store.create_goal('First goal', ['Evidence'], tmp_path)
    other = store.create_goal('Separate goal', ['Separate criteria'], tmp_path)
    service = Research(store)
    spec = ResearchPlan(scope=['system.md'], question='Which subsystem warrants investigation?')
    service.plan(first, 'discovery', spec)
    planned = service.plan(other, 'discovery', spec)
    assert not planned['duplicate']
    assert service.job(planned['job_id'])['goal_id'] == other
