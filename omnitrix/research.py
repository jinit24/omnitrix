"""Two research levels, independent evidence review, and durable budgets."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from typing import Annotated
from pydantic import Field
from agents import Agent, Runner, RunConfig, ModelSettings, function_tool
from .knowledge import Knowledge, ResearchTools, source_path, read_source_bytes
from .research_models import DiscoveryReport, ResearchReport, Evaluation, ResearchPlan
from .workers import ROOT


def research_tool_error(context, error):
    if isinstance(error, RuntimeError) and 'budget exhausted' in str(error):
        return 'Research tool budget exhausted. Stop gathering data and report the gap.'
    if isinstance(error, ValueError):
        return 'Invalid research tool arguments: '+str(error)
    return 'Research tool call failed. Read only explicit scoped files, with start_line >= 1 and line_count between 1 and 100. Check the supplied arguments before declaring source access blocked.'


class ResearchAgents:
    def __init__(self, model=None):
        self.model = model or os.getenv('OMNITRIX_MODEL', 'gpt-4.1')

    async def run(self, role, context, tools):
        @function_tool(failure_error_function=research_tool_error)
        async def read_source(locator: str, start_line: Annotated[int, Field(ge=1)] = 1, line_count: Annotated[int, Field(ge=1,le=100)] = 80) -> str:
            """Read 1 to 100 lines from an explicit scoped file; start_line must be positive. Returns captured evidence IDs and source revisions."""
            return json.dumps(tools.read(locator, start_line, line_count))

        @function_tool
        async def search_knowledge(query: str = '', offset: int = 0) -> str:
            """Reuse earlier system maps, rejected hypotheses and evaluated findings."""
            return json.dumps(tools.search(query, offset))

        output = {'discovery': DiscoveryReport, 'research': ResearchReport, 'evaluation': Evaluation}[role]
        agent = Agent(name=f'Independent {role} agent', model=self.model,
            instructions=(ROOT / f'instructions/{role.upper()}.md').read_text(),
            tools=[read_source, search_knowledge], output_type=output,
            model_settings=ModelSettings(max_tokens=3500))
        result = await Runner.run(agent, json.dumps(context), max_turns=8,
                                  run_config=RunConfig(tracing_disabled=True))
        from .telemetry import record_usage
        goal_id = tools.store.db.execute('SELECT goal_id FROM research_jobs WHERE id=(SELECT job_id FROM research_runs WHERE id=?)',(tools.run_id,)).fetchone()[0]
        record_usage(tools.store,goal_id,result,role)
        return result.final_output


class Research:
    def __init__(self, store, agents=None):
        self.store, self.db = store, store.db
        self.agents = agents or ResearchAgents()
        self.knowledge = Knowledge(store)

    def configure(self, goal_id, max_runs=12):
        if not 1 <= max_runs <= 1000:
            raise ValueError('Research run budget must be 1..1000')
        self.store.execute('INSERT OR IGNORE INTO research_limits(goal_id,max_runs) VALUES(?,?)',
                           (goal_id, max_runs))

    def charge(self, goal_id):
        self.configure(goal_id)
        result = self.store.execute('UPDATE research_limits SET spent=spent+1 WHERE goal_id=? AND spent<max_runs',
                                    (goal_id,))
        if result.rowcount != 1:
            raise RuntimeError('Goal research budget exhausted; escalate instead of retrying')

    def plan(self, goal_id, stage, spec):
        if stage not in ('discovery', 'research'):
            raise ValueError('Unknown research level')
        if stage == 'research' and not spec.hypothesis:
            raise ValueError('Focused research requires an explicit falsifiable hypothesis')
        if stage == 'discovery' and spec.hypothesis:
            raise ValueError('Discovery maps uncertainty; use research for a hypothesis')
        workspace = self.store.snapshot(goal_id)['goal']['workspace']
        scope = sorted({str(source_path(workspace, s).relative_to(__import__('pathlib').Path(workspace))) for s in spec.scope})
        spec = spec.model_copy(update={'scope': scope})
        if spec.opportunity_id:
            row = self.db.execute('SELECT goal_id FROM opportunities WHERE id=?', (spec.opportunity_id,)).fetchone()
            if row is None or row[0] != goal_id:
                raise ValueError('Opportunity must belong to this goal')
        from .control import Control
        revisions = Control(self.store).current(goal_id)
        signature = hashlib.sha256(json.dumps([workspace, stage, revisions['goal']['revision'], revisions['policy']['version'],
            ' '.join((spec.hypothesis or spec.question).casefold().split()), scope,
            [hashlib.sha256(read_source_bytes(workspace,s)[1]).hexdigest() for s in scope]], sort_keys=True).encode()).hexdigest()
        prior = self.db.execute('SELECT id,status FROM research_jobs WHERE signature=? ORDER BY created DESC LIMIT 1',
                                (signature,)).fetchone()
        if prior:
            return {'duplicate': True, 'job_id': prior['id'], 'status': prior['status'],
                    'feedback': 'Retrieve earlier findings; retry the existing job only with targeted feedback.'}
        job_id = uuid.uuid4().hex
        now = time.time()
        self.store.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            (job_id, goal_id, stage, signature, spec.model_dump_json(), 'pending', 0, now, now))
        return {'duplicate': False, 'job_id': job_id, 'status': 'pending'}

    def job(self, job_id):
        row = self.db.execute('SELECT * FROM research_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown research job')
        return dict(row)

    def finish(self, job, run_id, status, report=None, evaluation=None, feedback=''):
        with self.db:
            self.db.execute('UPDATE research_runs SET status=?,report=?,evaluation=?,feedback=?,finished=? WHERE id=?',
                (status, report.model_dump_json() if report else None,
                 evaluation.model_dump_json() if evaluation else None, feedback, time.time(), run_id))
            self.db.execute('UPDATE research_jobs SET status=?,updated=? WHERE id=?', (status, time.time(), job['id']))
            spec = json.loads(job['spec'])
            if spec.get('opportunity_id'):
                self.db.execute('UPDATE opportunities SET status=?,updated=? WHERE id=?',
                                 (status, time.time(), spec['opportunity_id']))
        return {'job_id': job['id'], 'run_id': run_id, 'status': status, 'feedback': feedback,
                'evaluation': evaluation.model_dump() if evaluation else None,
                'report': report.model_dump() if report else None}

    async def attempt(self, job_id, feedback=''):
        job = self.job(job_id)
        spec = ResearchPlan.model_validate_json(job['spec'])
        if job['status'] in ('supported', 'falsified', 'escalated', 'discovered', 'invalidated', 'stale'):
            return {'job_id': job_id, 'status': job['status'], 'feedback': 'Terminal job; reuse knowledge or replan.'}
        if job['status'] in ('insufficient','blocked','interrupted') and job['attempts'] and not feedback.strip():
            raise ValueError('A retry needs targeted feedback, not an unchanged repeat')
        if job['attempts'] >= spec.budget.attempts:
            self.store.execute("UPDATE research_jobs SET status='escalated' WHERE id=?", (job_id,))
            return {'job_id': job_id, 'status': 'escalated', 'feedback': 'Attempt budget exhausted'}
        try:
            self.charge(job['goal_id'])
        except RuntimeError as exc:
            self.store.execute("UPDATE research_jobs SET status='escalated' WHERE id=?",(job_id,))
            return {'job_id':job_id,'status':'escalated','feedback':str(exc)}
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO research_runs(id,job_id,status,created) VALUES(?,?,?,?)',
                            (run_id, job_id, 'running', time.time()))
            self.db.execute("UPDATE research_jobs SET attempts=attempts+1,status='running' WHERE id=?", (job_id,))
        workspace = self.store.snapshot(job['goal_id'])['goal']['workspace']
        from .control import Control
        revision = Control(self.store).current(job['goal_id'])
        try:
            tools = ResearchTools(self.store, run_id, workspace, spec.scope, spec.budget.tool_calls)
            # Mandatory retrieval happens before any new investigation, even if the model omits it.
            existing = tools.prefetch()
            context = {'plan': spec.model_dump(), 'prior_knowledge': existing, 'feedback': feedback,
                       'goal': self.store.snapshot(job['goal_id'])['goal']}
            report = await asyncio.wait_for(self.agents.run(job['stage'], context, tools), spec.budget.seconds)
            if Control(self.store).current(job['goal_id']) != revision or self.job(job_id)['status'] == 'invalidated':
                return self.finish(job, run_id, 'stale', feedback='Goal or policy changed during research; retained report is not accepted evidence')
            if job['stage'] == 'discovery':
                report = DiscoveryReport.model_validate(report)
                self.save_discovery(job, report, tools)
                return self.finish(job, run_id, 'discovered', report)
            report = ResearchReport.model_validate(report)
            # Evaluation has a fresh agent context, captured excerpts, and its own bounded reads.
            self.charge(job['goal_id'])
            review_tools = ResearchTools(self.store, run_id, workspace, spec.scope, spec.budget.tool_calls)
            evidence = tools.evidence()
            evaluation = await asyncio.wait_for(self.agents.run('evaluation',
                {'plan': spec.model_dump(), 'report': report.model_dump(), 'evidence': evidence}, review_tools),
                spec.budget.seconds)
            evaluation = Evaluation.model_validate(evaluation)
            if Control(self.store).current(job['goal_id']) != revision or self.job(job_id)['status'] == 'invalidated':
                return self.finish(job, run_id, 'stale', report, feedback='Revision changed during evaluation')
            evaluation = self.gate(report, evaluation, review_tools.evidence())
            status = evaluation.verdict
            if status in ('insufficient', 'blocked') and job['attempts'] + 1 >= spec.budget.attempts:
                status = 'escalated'
            self.save_findings(job, report, evaluation)
            return self.finish(job, run_id, status, report, evaluation, evaluation.feedback)
        except asyncio.CancelledError:
            self.finish(job, run_id, 'interrupted', feedback='Cancelled; reconcile captured observations before retry')
            raise
        except Exception as exc:
            status = 'escalated' if job['attempts'] + 1 >= spec.budget.attempts else 'blocked'
            if 'budget exhausted' in str(exc):
                status = 'escalated'
            return self.finish(job, run_id, status, feedback=f'{type(exc).__name__}: {exc}')

    @staticmethod
    def gate(report, evaluation, evidence):
        available = {e['id']: e for e in evidence}
        cited = set(report.supporting_evidence + report.contradictory_evidence + evaluation.evidence_ids)
        claims = [f for f in report.findings if f.classification == 'claim']
        cited.update(s for finding in report.findings for s in finding.evidence_ids)
        errors = []
        if any(s not in available or available[s]['stale'] for s in cited):
            errors.append('Unknown or stale evidence IDs; read current scoped sources')
        if evaluation.verdict in ('supported', 'falsified') and not evaluation.evidence_ids:
            errors.append('Evaluator must cite independently available evidence')
        if evaluation.verdict == 'supported' and (not claims or any(not f.evidence_ids for f in claims)):
            errors.append('Every asserted claim needs observed evidence; assumptions are not verified facts')
        if report.blocked and evaluation.verdict == 'supported':
            errors.append('Blocked research cannot establish support')
        if errors:
            return Evaluation(verdict='insufficient', evidence_ids=[], reason='; '.join(errors),
                              feedback='; '.join(errors))
        return evaluation

    def save_discovery(self, job, report, tools):
        available = {s['id']: s for s in tools.evidence()}
        for entry in report.system_map:
            citations = [s for s in entry.evidence_ids if s in available and not available[s]['stale']]
            # Discovery is reconnaissance, not independent verification.
            self.knowledge.put(job['goal_id'], job['id'], 'system_map', entry.name,
                entry.model_dump(), 'assumption' if entry.assumption or not citations else 'observed', citations)
        for opportunity in report.opportunities:
            citations = [s for s in opportunity.evidence_ids if s in available and not available[s]['stale']]
            confidence = opportunity.confidence if citations else 0
            score = opportunity.impact * confidence / (opportunity.risk * opportunity.effort)
            opportunity = opportunity.model_copy(update={'confidence': confidence, 'evidence_ids': citations})
            # Repeated discovery should update the backlog rather than multiply entries.
            prior = self.db.execute('SELECT id FROM opportunities WHERE goal_id=? AND title=?',
                                    (job['goal_id'], opportunity.title)).fetchone()
            if not prior:
                now = time.time()
                self.store.execute('INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, job['goal_id'], job['id'], opportunity.title,
                     opportunity.model_dump_json(), score, 'open', now, now))
            else:
                self.store.execute('UPDATE opportunities SET spec=?,score=?,updated=? WHERE id=?',
                    (opportunity.model_dump_json(),score,time.time(),prior['id']))
        self.knowledge.put(job['goal_id'], job['id'], 'discovery_gaps', 'Discovery gaps',
                           report.gaps, 'assumption', [])

    def save_findings(self, job, report, evaluation):
        for finding in report.findings:
            classification = 'verified' if evaluation.verdict == 'supported' and set(finding.evidence_ids).issubset(evaluation.evidence_ids) and finding.classification == 'claim' else 'assumption'
            self.knowledge.put(job['goal_id'], job['id'], 'finding', finding.statement,
                {'finding': finding.model_dump(), 'evaluation': evaluation.model_dump(),
                 'uncertainties': report.uncertainties}, classification,
                [s for s in finding.evidence_ids if self.db.execute('SELECT 1 FROM research_sources WHERE id=?', (s,)).fetchone()])
        if evaluation.verdict == 'falsified':
            self.knowledge.put(job['goal_id'], job['id'], 'rejected_hypothesis', json.loads(job['spec'])['hypothesis'],
                {'reason': evaluation.reason, 'uncertainties': report.uncertainties}, 'rejected', evaluation.evidence_ids)
