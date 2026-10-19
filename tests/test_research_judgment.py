"""Independent review sees the contract; unresolved assumptions stay unverified."""
from omnitrix.control import Control
from omnitrix.research import Research
from omnitrix.research_models import Evaluation, Finding, ResearchPlan, ResearchReport
from omnitrix.store import Store
from tests_helpers import FixtureAgents


async def test_review_receives_original_and_current_requirements(tmp_path):
    (tmp_path / 'system.md').write_text('Queue polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Investigate queue latency', ['Preserve delivery'], tmp_path)
    control = Control(store)
    proposal = control.propose(goal, 'requirements', {
        'constraint': 'No production changes', 'add_requirements': ['Bound investigation time']})
    control.accept(goal, proposal)

    class InspectingAgents(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'evaluation':
                contract = context['goal']
                assert contract['original_requirements'] == ['Preserve delivery']
                assert contract['requirements'] == ['Preserve delivery', 'Bound investigation time']
                assert contract['constraints'] == ['No production changes']
                assert contract['revision'] == 2
            return await super().run(role, context, tools)

    service = Research(store, InspectingAgents())
    plan = ResearchPlan(scope=['system.md'], question='Does polling cause the stated delay?',
        hypothesis='Queue polling interval is 30 seconds', falsification='Interval is not 30 seconds')
    job = service.plan(goal, 'research', plan)['job_id']
    assert (await service.attempt(job))['status'] == 'supported'


def test_supported_defect_does_not_verify_proposed_repair():
    report = ResearchReport(findings=[
        Finding(statement='Current code calls once despite the retry contract',
            classification='claim', evidence_ids=['current']),
        Finding(statement='A bounded retry loop should repair the defect',
            classification='assumption', evidence_ids=[])],
        supporting_evidence=['current'], contradictory_evidence=[],
        uncertainties=['Repair is not implemented or tested', 'Nonpositive attempts unspecified'],
        recommendation='Authorize bounded implementation and execute the acceptance tests', blocked=False)
    review = Evaluation(verdict='supported', evidence_ids=['current'],
        reason='The current defect is evidenced; future repair is an assumption',
        feedback='Implement and independently verify all existing criteria')
    assert Research.gate(report, review, [{'id': 'current', 'stale': False}]).verdict == 'supported'
    report.findings[1].classification = 'claim'
    rejected = Research.gate(report, review, [{'id': 'current', 'stale': False}])
    assert rejected.verdict == 'insufficient'
    assert report.findings[1].statement in rejected.feedback


async def test_revision_change_after_review_cannot_publish_stale_findings(tmp_path, monkeypatch):
    (tmp_path / 'system.md').write_text('Queue polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Investigate queue latency', ['Preserve delivery'], tmp_path)
    service = Research(store, FixtureAgents())
    plan = ResearchPlan(scope=['system.md'], question='Does polling cause the stated delay?',
        hypothesis='Queue polling interval is 30 seconds', falsification='Interval is not 30 seconds')
    job = service.plan(goal, 'research', plan)['job_id']
    other = Store(store.path)
    control = Control(other)
    proposal = control.propose(goal, 'requirements', {'constraint': 'Preserve ordering'})

    def concurrent_change(report, evaluation, evidence, plan=None, coverage=None):
        # A different connection commits after the first revision check.
        control.accept(goal, proposal)
        return Research.gate(report, evaluation, evidence, plan, coverage)

    monkeypatch.setattr(service, 'gate', concurrent_change)
    result = await service.attempt(job)
    assert result['status'] == 'stale'
    assert not store.db.execute(
        "SELECT 1 FROM knowledge WHERE job_id=? AND classification='verified'", (job,)).fetchone()


async def test_revision_change_before_discovery_publication_stays_unaccepted(tmp_path, monkeypatch):
    from omnitrix.knowledge import ResearchTools
    (tmp_path / 'system.md').write_text('Queue polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Investigate queue latency', ['Preserve delivery'], tmp_path)
    service = Research(store, FixtureAgents())
    job = service.plan(goal, 'discovery', ResearchPlan(
        scope=['system.md'], question='Which queue behavior warrants investigation?'))['job_id']
    control = Control(Store(store.path))
    proposal = control.propose(goal, 'requirements', {'constraint': 'Preserve ordering'})
    evidence = ResearchTools.evidence

    def change_after_observation(tools):
        sources = evidence(tools)
        control.accept(goal, proposal)
        return sources

    monkeypatch.setattr(ResearchTools, 'evidence', change_after_observation)
    assert (await service.attempt(job))['status'] == 'stale'
    assert not store.db.execute('SELECT 1 FROM knowledge WHERE job_id=?', (job,)).fetchone()
    assert not store.db.execute('SELECT 1 FROM opportunities WHERE job_id=?', (job,)).fetchone()


async def test_manager_receives_partial_falsification_and_preserved_contract(tmp_path, monkeypatch):
    import json
    from omnitrix.manager import AgentBrain
    from omnitrix.models import Decision
    (tmp_path / 'service.py').write_text('def process(call, attempts=3):\n    return call()\n')
    (tmp_path / 'contract.txt').write_text('Retry TimeoutError; propagate ValueError immediately.\n')
    store = Store(tmp_path / 'state.db')
    criteria = ['Retry transient timeouts', 'Preserve permanent-error propagation']
    goal = store.create_goal('Repair retry behavior', criteria, tmp_path)

    class PartialFindings:
        async def run(self, role, context, tools):
            if role == 'evaluation':
                return Evaluation(verdict='falsified', evidence_ids=context['report']['supporting_evidence'],
                    test_kind='counterexample',test_evidence_ids=context['report']['supporting_evidence'],
                    reason='The compound claim is false: permanent errors already propagate',
                    feedback='Investigate the missing TimeoutError retries while preserving permanent errors')
            sources = [tools.read('service.py')['id'], tools.read('contract.txt')['id']]
            return ResearchReport(findings=[
                Finding(statement='Current process lacks TimeoutError retries', classification='claim', evidence_ids=sources),
                Finding(statement='Permanent errors already propagate immediately', classification='claim', evidence_ids=sources)],
                supporting_evidence=sources, contradictory_evidence=sources,
                uncertainties=[], recommendation='Narrow the hypothesis to missing timeout retries', blocked=False)

    service = Research(store, PartialFindings())
    job = service.plan(goal, 'research', ResearchPlan(scope=['service.py', 'contract.txt'],
        question='Does current process violate both retry and permanent-error behavior?',
        hypothesis='Current process violates both timeout retries and permanent-error propagation',
        falsification='Either required behavior already works'))['job_id']
    outcome = await service.attempt(job)
    assert outcome['status'] == 'falsified'
    Control(store).message(goal, 'findings', outcome, direction='worker_to_manager')

    async def inspect_prompt(agent, prompt, **kwargs):
        state = json.loads(prompt)
        assert state['goal']['requirements'] == criteria
        handoff = json.loads(state['worker_messages'][0]['body'])
        assert handoff['report']['findings'][1]['statement'] == 'Permanent errors already propagate immediately'
        assert 'missing TimeoutError retries' in handoff['evaluation']['feedback']
        assert 'A falsified conjunction does not falsify the goal' in agent.instructions
        # Guidance supplies evidence without replacing the agent's decision.
        class Result:
            final_output = Decision(action='pause', rationale='Test only: decision remains model-owned',
                instructions='No execution requested by this context test', task_id=None, priority=1)
        return Result()

    monkeypatch.setattr('omnitrix.manager.Runner.run', inspect_prompt)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action == 'pause'
