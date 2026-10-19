"""Two research levels, independent evidence review, and durable budgets."""
import asyncio
import csv
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated
from pydantic import Field
from agents import Agent, Runner, RunConfig, ModelSettings, function_tool
from .knowledge import Knowledge, ResearchTools, source_path, source_revision
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

        @function_tool(failure_error_function=research_tool_error)
        async def tail_source(locator:str,line_count:Annotated[int,Field(ge=1,le=100)]=80,before_line:Annotated[int,Field(ge=1)]|None=None)->str:
            """Read the last 1..100 lines of a scoped source. Useful for end-of-run log summaries; follow next_before backwards. Evidence includes actual line numbers."""
            return json.dumps(tools.tail(locator,line_count,before_line))

        @function_tool(failure_error_function=research_tool_error)
        async def search_knowledge(query: str = '', offset: int = 0) -> str:
            """Reuse earlier system maps, rejected hypotheses and evaluated findings."""
            return json.dumps(tools.search(query, offset))

        @function_tool(failure_error_function=research_tool_error)
        async def search_source(locator: str, query: str, start_line: Annotated[int, Field(ge=1)] = 1) -> str:
            """Search one scoped file for a literal string. Scans at most 5000 lines/512 KiB; follow next_line for further pages. Results have citable evidence IDs."""
            return json.dumps(tools.search_source(locator, query, start_line))

        @function_tool(failure_error_function=research_tool_error)
        async def summarize_csv(locator: str, value_column: str, group_by: str | None = None, start_row: Annotated[int, Field(ge=1)] = 1) -> str:
            """Aggregate a numeric CSV column by an optional group. Automatically follows 5000-row/512-KiB pages while budget remains; each page spends a tool call. Follow summary.next_row if partial; partial totals are not whole-file totals."""
            return json.dumps(tools.summarize_csv(locator, value_column, group_by, start_row, complete=True))

        output = {'discovery': DiscoveryReport, 'research': ResearchReport, 'evaluation': Evaluation}[role]
        catalog = []
        numeric_csv = False
        for locator in sorted(tools.scope):
            path = source_path(tools.workspace, locator)
            item = {'locator': locator, 'size_bytes': path.stat().st_size}
            if path.suffix.lower() == '.csv':
                from .knowledge import source_lines
                rows = csv.DictReader(source_lines(path), strict=True)
                sample = next(rows, {})
                item['columns'] = rows.fieldnames
                item['numeric_sample_columns'] = []
                for name, value in sample.items():
                    try:
                        float(value)
                    except (ValueError, TypeError):
                        continue
                    item['numeric_sample_columns'].append(name)
                numeric_csv |= bool(item['numeric_sample_columns'])
            catalog.append(item)
        context = context | {'source_catalog': catalog}
        choice = 'auto' if role == 'evaluation' else 'summarize_csv' if numeric_csv else 'required'
        from .spending import metered_model
        goal_id = tools.store.db.execute('SELECT goal_id FROM research_jobs WHERE id=(SELECT job_id FROM research_runs WHERE id=?)',(tools.run_id,)).fetchone()[0]
        agent = Agent(name=f'Independent {role} agent', model=metered_model(tools.store, goal_id, role, self.model),
            instructions=(ROOT / f'instructions/{role.upper()}.md').read_text(),
            tools=[read_source, search_knowledge, search_source, summarize_csv,tail_source], output_type=output,
            model_settings=ModelSettings(max_tokens=3500,
                tool_choice=choice))
        result = await Runner.run(agent, json.dumps(context), max_turns=max(8, min(28, tools.remaining + 2)),
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
        if not 1 <= max_runs <= 100000:
            raise ValueError('Research run budget must be 1..100000')
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
        scope = []
        for locator in spec.scope:
            try:
                scope.append(str(source_path(workspace, locator).relative_to(Path(workspace))))
            except ValueError as exc:
                from .frontier import Frontier
                catalog=Frontier(self.store,goal_id).page(prefix=locator,limit=5)
                parent=Path(locator).parent
                while not catalog['entries'] and str(parent)!='.':
                    catalog=Frontier(self.store,goal_id).page(prefix=str(parent),limit=5)
                    parent=parent.parent
                raise ValueError(f'Invalid scope {locator!r}: {exc}. Scope must contain explicit regular files. '
                    'Choose relevant leaf paths using list_resources/search_frontier; directory scope is invalid. '
                    'Indexed descendant examples: '+json.dumps(catalog)) from None
        scope = sorted(set(scope))
        spec = spec.model_copy(update={'scope': scope})
        if spec.opportunity_id:
            row = self.db.execute('SELECT goal_id FROM opportunities WHERE id=?', (spec.opportunity_id,)).fetchone()
            if row is None or row[0] != goal_id:
                raise ValueError('Opportunity must belong to this goal')
        from .control import Control
        revisions = Control(self.store).current(goal_id)
        signature = hashlib.sha256(json.dumps(['hypothesis-proof-v2',goal_id, workspace, stage, revisions['goal']['revision'], revisions['policy']['version'],
            ' '.join((spec.hypothesis or spec.question).casefold().split()), scope,
            [source_revision(workspace,s)[1] for s in scope]], sort_keys=True).encode()).hexdigest()
        prior = self.db.execute('SELECT id,status FROM research_jobs WHERE goal_id=? AND signature=? ORDER BY created DESC LIMIT 1',
                                (goal_id, signature)).fetchone()
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
        report=None
        try:
            tools = ResearchTools(self.store, run_id, workspace, spec.scope, spec.budget.tool_calls)
            # Mandatory retrieval happens before any new investigation, even if the model omits it.
            existing = tools.prefetch()
            context = {'plan': spec.model_dump(), 'prior_knowledge': existing, 'feedback': feedback,
                       'goal': self.store.snapshot(job['goal_id'])['goal'],
                       'research_progress': self.knowledge.progress(job['goal_id'], limit=40)}
            report = await asyncio.wait_for(self.agents.run(job['stage'], context, tools), spec.budget.seconds)
            if Control(self.store).current(job['goal_id']) != revision or self.job(job_id)['status'] == 'invalidated':
                return self.finish(job, run_id, 'stale', feedback='Goal or policy changed during research; retained report is not accepted evidence')
            if job['stage'] == 'discovery':
                report = DiscoveryReport.model_validate(report)
                if not any(not source['stale'] for source in tools.evidence()):
                    raise ValueError('Discovery inspected no current evidence. Read scoped sources before returning a map; filename assumptions alone do not complete discovery.')
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
            original=evaluation
            evaluation = self.gate(report, evaluation, review_tools.evidence(), spec,
                                   review_tools.coverage(evaluation.test_evidence_ids))
            if original.verdict in ('supported','falsified') and evaluation.verdict!=original.verdict:
                # Repair a rejected review once, without rerunning valid research.
                # A genuinely insufficient scientific result is not auto-retried.
                Control(self.store).event(job['goal_id'],'review_gate_rejected',
                    {'job_id':job_id,'review':original.model_dump(),'gate_feedback':evaluation.feedback})
                self.charge(job['goal_id'])
                review_tools=ResearchTools(self.store,run_id,workspace,spec.scope,spec.budget.tool_calls)
                corrected=await asyncio.wait_for(self.agents.run('evaluation',
                    {'plan':spec.model_dump(),'report':report.model_dump(),'evidence':review_tools.evidence(),
                     'gate_feedback':evaluation.feedback,'instruction':'Make a fresh, internally consistent judgment. Do not change the verdict merely to pass a gate; use insufficient if evidence is inadequate.'},review_tools),spec.budget.seconds)
                corrected=Evaluation.model_validate(corrected)
                evaluation=self.gate(report,corrected,review_tools.evidence(),spec,review_tools.coverage(corrected.test_evidence_ids))
                Control(self.store).event(job['goal_id'],'review_gate_correction',
                    {'job_id':job_id,'review':corrected.model_dump(),'accepted_verdict':evaluation.verdict})
            if Control(self.store).current(job['goal_id'])!=revision or self.job(job_id)['status']=='invalidated':
                return self.finish(job,run_id,'stale',report,feedback='Revision changed during review correction')
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
            return self.finish(job, run_id, status, report if isinstance(report,ResearchReport) else None,
                               feedback=f'{type(exc).__name__}: {exc}')

    @staticmethod
    def gate(report, evaluation, evidence, plan=None, coverage=None):
        available = {e['id']: e for e in evidence}
        cited = set(report.supporting_evidence + report.contradictory_evidence + evaluation.evidence_ids + evaluation.test_evidence_ids + evaluation.counterevidence_ids)
        cited.update(source for review in evaluation.claim_reviews for source in review.evidence_ids)
        claims = [f for f in report.findings if f.classification == 'claim']
        cited.update(s for finding in report.findings for s in finding.evidence_ids)
        errors = []
        if any(s not in available or available[s]['stale'] for s in cited):
            errors.append('Unknown or stale evidence IDs; read current scoped sources')
        if evaluation.verdict in ('supported', 'falsified') and not evaluation.evidence_ids:
            errors.append('Evaluator must cite independently available evidence')
        if evaluation.verdict == 'supported' and (not claims or any(not f.evidence_ids for f in claims)):
            errors.append('Every asserted claim needs observed evidence; assumptions are not verified facts')
        if evaluation.verdict=='supported' and evaluation.claim_reviews:
            expected={index for index,finding in enumerate(report.findings) if finding.classification=='claim'}
            reviewed={review.finding_index for review in evaluation.claim_reviews}
            if reviewed!=expected or len(evaluation.claim_reviews)!=len(expected):
                errors.append('Every factual finding needs exactly one explicit claim review')
            for review in evaluation.claim_reviews:
                if review.finding_index>=len(report.findings):continue
                original=report.findings[review.finding_index]
                def binds(candidate,prior):
                    if candidate==prior:return True
                    a,b=available.get(candidate,{}),available.get(prior,{})
                    return bool(a.get('locator') and a.get('locator')==b.get('locator') and a.get('revision')==b.get('revision')
                        and b.get('excerpt') and b['excerpt'] in a.get('excerpt',''))
                if not review.supported or not any(binds(source,prior) for source in review.evidence_ids for prior in original.evidence_ids):
                    errors.append('Claim review is unsupported or bound to unrelated evidence')
        elif evaluation.verdict == 'supported' and any(not set(f.evidence_ids).issubset(evaluation.evidence_ids) for f in claims):
            errors.append('Evaluator evidence must cover every factual claim, not unrelated observations')
        if report.blocked and evaluation.verdict == 'supported':
            errors.append('Blocked research cannot establish support')
        if plan and evaluation.verdict in ('supported','falsified'):
            proof=set(evaluation.test_evidence_ids)
            if not proof or not proof.issubset(evaluation.evidence_ids) or evaluation.test_kind=='not_tested':
                errors.append('A terminal verdict needs explicit evidence testing the hypothesis itself, not just individual report claims')
            if evaluation.verdict=='falsified' and (evaluation.test_kind not in ('counterexample','measurement') or not proof.intersection(report.contradictory_evidence+evaluation.counterevidence_ids)):
                errors.append('Falsification needs observed counterevidence. Missing measurements or absent support is insufficient')
            if evaluation.verdict=='supported' and evaluation.test_kind in ('absence','literal_absence'):
                coverage=coverage or {}
                if evaluation.test_kind=='absence':
                    missing=[s for s in plan.scope if not coverage.get(s,{}).get('read_complete')]
                else:
                    query=(evaluation.literal_query or '').strip().casefold()
                    missing=[s for s in plan.scope if not query or query not in coverage.get(s,{}).get('literal_absence',[])]
                if missing:
                    errors.append('Absence claim lacks complete cited scope coverage: '+', '.join(missing))
                if any(f.category=='mechanism' for f in claims):
                    errors.append('An absence search alone cannot verify an inefficiency mechanism')
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
        reviews={review.finding_index:review for review in evaluation.claim_reviews}
        for index,finding in enumerate(report.findings):
            review=reviews.get(index)
            approved=(review and review.supported) or set(finding.evidence_ids).issubset(evaluation.evidence_ids)
            classification = 'verified' if evaluation.verdict == 'supported' and approved and finding.classification == 'claim' else 'assumption'
            if classification=='verified' and review:
                finding=finding.model_copy(update={'evidence_ids':review.evidence_ids})
            self.knowledge.put(job['goal_id'], job['id'], 'finding', finding.statement,
                {'finding': finding.model_dump(), 'evaluation': evaluation.model_dump(),
                 'uncertainties': report.uncertainties}, classification,
                [s for s in finding.evidence_ids if self.db.execute('SELECT 1 FROM research_sources WHERE id=?', (s,)).fetchone()])
        if evaluation.verdict == 'falsified':
            self.knowledge.put(job['goal_id'], job['id'], 'rejected_hypothesis', json.loads(job['spec'])['hypothesis'],
                {'reason': evaluation.reason, 'uncertainties': report.uncertainties}, 'rejected', evaluation.evidence_ids)
