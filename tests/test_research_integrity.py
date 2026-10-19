"""Acceptance must bind review evidence and goal ownership correctly."""
from omnitrix.research import Research
from omnitrix.research_models import ClaimReview, Evaluation, Finding, ResearchPlan, ResearchReport
from omnitrix.store import Store


def test_supported_verdict_must_cover_every_claim_not_an_unrelated_citation():
    report = ResearchReport(findings=[Finding(statement='Database is the bottleneck', classification='claim', evidence_ids=['db'])],
        supporting_evidence=['db'], contradictory_evidence=[], uncertainties=[], recommendation='Batch reads', blocked=False)
    evaluation = Evaluation(verdict='supported', evidence_ids=['cache'], reason='Cache warning observed', feedback='Proceed')
    result = Research.gate(report, evaluation, [{'id': 'db', 'stale': False}, {'id': 'cache', 'stale': False}])
    assert result.verdict == 'insufficient'


def test_explicit_claim_reviews_select_relevant_citations_without_losing_claim_binding():
    report = ResearchReport(findings=[
        Finding(statement='Database costs 600 ms', classification='claim', evidence_ids=['db', 'catalog', 'cache']),
        Finding(statement='Cache costs 10 ms', classification='claim', evidence_ids=['cache'])],
        supporting_evidence=['db', 'cache'], contradictory_evidence=[], uncertainties=[], recommendation='Measure batching', blocked=False)
    evidence=[{'id':source,'stale':False} for source in ('db','catalog','cache')]
    review=Evaluation(verdict='supported',evidence_ids=['db','cache'],reason='Both totals independently checked',feedback='Proceed',
        claim_reviews=[ClaimReview(finding_index=0,reason='DB measurement supplies the total',evidence_ids=['db'],supported=True),
                       ClaimReview(finding_index=1,reason='Cache measurement supplies its total',evidence_ids=['cache'],supported=True)])
    assert Research.gate(report,review,evidence).verdict=='supported'
    assert Research.gate(report,review.model_copy(update={'claim_reviews':review.claim_reviews[:1]}),evidence).verdict=='insufficient'
    unrelated=review.claim_reviews[1].model_copy(update={'evidence_ids':['db']})
    assert Research.gate(report,review.model_copy(update={'claim_reviews':[review.claim_reviews[0],unrelated]}),evidence).verdict=='insufficient'


def test_claim_review_can_bind_a_fresh_broader_observation_of_the_same_source():
    report=ResearchReport(findings=[Finding(statement='A counter exists',classification='claim',evidence_ids=['original'])],
        supporting_evidence=['original'],contradictory_evidence=[],uncertainties=[],recommendation='Inspect counter',blocked=False)
    review=Evaluation(verdict='supported',evidence_ids=['reviewed'],reason='Counter independently read',feedback='Proceed',
        claim_reviews=[ClaimReview(finding_index=0,reason='Same observed counter',evidence_ids=['reviewed'],supported=True)])
    evidence=[{'id':'original','stale':False,'locator':'counter.py','revision':'same','excerpt':'1: counter = 1'},
              {'id':'reviewed','stale':False,'locator':'counter.py','revision':'same','excerpt':'1: counter = 1\n2: emit(counter)'}]
    assert Research.gate(report,review,evidence).verdict=='supported'
    evidence[1]['revision']='other'
    assert Research.gate(report,review,evidence).verdict=='insufficient'


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
