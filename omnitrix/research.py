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
from .research_models import DiscoveryReport, ResearchReport, Evaluation, ResearchPlan, DependencySource
from .workers import ROOT
from .budget import budget_run_config, close_budget_run_config


def research_tool_error(context, error):
    if isinstance(error, RuntimeError) and ('reserved for required checkpoint' in str(error) or 'time budget exhausted' in str(error)):
        return str(error) + '. Record the required checkpoint if time remains; then report the typed stop reason and remaining gaps.'
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

        @function_tool
        async def checkpoint(evidence_ids: list[str], uncertainty: str, next_step: str) -> str:
            """Record a research checkpoint: evidence gained, remaining uncertainty and next action. Does not extend budgets."""
            return json.dumps(tools.checkpoint(evidence_ids, uncertainty, next_step))

        @function_tool(failure_error_function=research_tool_error)
        async def locate_source(locator: str, query: str, offset: int = 0) -> str:
            """Find literal text in an explicit scoped source; read matching lines for citable evidence."""
            return json.dumps(tools.locate(locator, query, offset))

        @function_tool(failure_error_function=research_tool_error)
        async def request_dependency(callsite_evidence_ids: list[str], reason: str, requests: list[DependencySource]) -> str:
            """Request up to two prerequisite files within preapproved roots, citing captured callsites. Never extends budgets."""
            return json.dumps(tools.request_dependency(callsite_evidence_ids, reason, [item.model_dump() for item in requests]))

        output = {'discovery': DiscoveryReport, 'research': ResearchReport, 'evaluation': Evaluation}[role]
        agent = Agent(name=f'Independent {role} agent', model=self.model,
            instructions=(ROOT / f'instructions/{role.upper()}.md').read_text(),
            tools=[read_source, search_knowledge, checkpoint, locate_source, request_dependency], output_type=output,
            model_settings=ModelSettings(max_tokens=3500))
        goal_id = tools.store.db.execute('SELECT goal_id FROM research_jobs WHERE id=(SELECT job_id FROM research_runs WHERE id=?)',(tools.run_id,)).fetchone()[0]
        run_config = budget_run_config(tools.store, goal_id, role, model=self.model,
                                      review_id=context.get('review_budget_id') if role == 'evaluation' else None)
        try:
            result = await Runner.run(agent, json.dumps(context), max_turns=min(32, tools.remaining + 2),
                                      run_config=run_config)
        finally:
            await close_budget_run_config(run_config)
        from .telemetry import record_usage
        record_usage(tools.store,goal_id,result,role)
        return result.final_output


class Research:
    def __init__(self, store, agents=None, require_checkpoint=False):
        self.store, self.db = store, store.db
        self.agents = agents or ResearchAgents()
        self.knowledge = Knowledge(store)
        self.require_checkpoint = bool(require_checkpoint)

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

    def preflight(self, goal_id, stage, spec):
        """Read-only plan admission; safe to run for an entire batch before inserting jobs."""
        from .control import Control
        from .planning import preflight_plan
        spec = ResearchPlan.model_validate(spec)
        revisions = Control(self.store).current(goal_id)
        context = revisions['research_context']
        if context['version'] and any(not getattr(spec, key).strip() for key in ('decision','expected_value','stop_condition')):
            raise ValueError('Context-configured research needs decision, expected_value and stop_condition')
        if context.get('require_evidence_plan'):
            kinds = {item.kind for item in spec.evidence_obligations if item.required}
            external = any(not item.required and item.reason.strip() for item in spec.unresolved_dependencies)
            if not spec.primary_area or 'implementation' not in kinds or (not kinds.intersection({'test','contract','final_outcome'}) and not external):
                raise ValueError('Evidence-plan context requires registered primary_area, implementation and test/contract/final_outcome obligations or an explicit external prerequisite')
        if spec.opportunity_id:
            row = self.db.execute('SELECT goal_id FROM opportunities WHERE id=?', (spec.opportunity_id,)).fetchone()
            if row is None or row[0] != goal_id:
                raise ValueError('Opportunity must belong to this goal')
        return preflight_plan(self.store, goal_id, stage, spec,
            self.require_checkpoint or context.get('require_checkpoint', False))

    def plan(self, goal_id, stage, spec):
        admission = self.preflight(goal_id, stage, spec)
        spec, proof = admission['spec'], admission['proof']
        workspace = self.store.snapshot(goal_id)['goal']['workspace']
        scope = spec.scope
        from .control import Control
        revisions = Control(self.store).current(goal_id)
        signature_input = [workspace, stage, revisions['goal']['revision'], revisions['policy']['version'],
            ' '.join((spec.hypothesis or spec.question).casefold().split()), scope,
            [hashlib.sha256(read_source_bytes(workspace,s)[1]).hexdigest() for s in scope]]
        if spec.primary_area or spec.evidence_obligations or spec.unresolved_dependencies or spec.allow_scope_expansion or spec.dependency_roots:
            signature_input.append([spec.primary_area, [item.model_dump() for item in spec.evidence_obligations],
                [item.model_dump() for item in spec.unresolved_dependencies], spec.allow_scope_expansion, spec.dependency_roots])
        signature = hashlib.sha256(json.dumps(signature_input, sort_keys=True).encode()).hexdigest()
        prior = self.db.execute('SELECT id,status FROM research_jobs WHERE goal_id=? AND signature=? ORDER BY created DESC LIMIT 1',
                                (goal_id, signature)).fetchone()
        if prior:
            return {'duplicate': True, 'job_id': prior['id'], 'status': prior['status'],
                    'feedback': 'Retrieve earlier findings; retry the existing job only with targeted feedback.'}
        job_id = uuid.uuid4().hex
        now = time.time()
        self.store.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            (job_id, goal_id, stage, signature, spec.model_dump_json(), 'pending', 0, now, now))
        self.store.execute('INSERT INTO research_preflights VALUES(?,?,?)', (job_id, json.dumps(proof), now))
        return {'duplicate': False, 'job_id': job_id, 'status': 'pending'}

    def record_audit_result(self, job):
        if not json.loads(job['spec']).get('primary_area'):
            return None
        try:
            from .audit import Audit
            result = Audit(self.store, job['goal_id']).record_result(job['id'])
        except Exception as exc:
            result = {'recorded': False, 'reason': f'{type(exc).__name__}: {exc}'}
        if not result.get('recorded'):
            from .control import Control
            Control(self.store).event(job['goal_id'], 'audit_sample_not_recorded', {'job_id': job['id'], **result})
        return result

    def job(self, job_id):
        row = self.db.execute('SELECT * FROM research_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown research job')
        return dict(row)

    def finish(self, job, run_id, status, report=None, evaluation=None, feedback=''):
        with self.db:
            self.db.execute('UPDATE research_runs SET status=?,report=COALESCE(?,report),evaluation=COALESCE(?,evaluation),feedback=?,finished=? WHERE id=?',
                (status, report.model_dump_json() if report else None,
                 evaluation.model_dump_json() if evaluation else None, feedback, time.time(), run_id))
            self.db.execute('UPDATE research_jobs SET status=?,updated=? WHERE id=?', (status, time.time(), job['id']))
            spec = json.loads(job['spec'])
            if spec.get('opportunity_id'):
                self.db.execute('UPDATE opportunities SET status=?,updated=? WHERE id=?',
                                 (status, time.time(), spec['opportunity_id']))
        saved = self.db.execute('SELECT report,evaluation FROM research_runs WHERE id=?', (run_id,)).fetchone()
        audit_result = self.record_audit_result(job) if status in ('supported','falsified') else None
        return {'job_id': job['id'], 'run_id': run_id, 'status': status, 'feedback': feedback, 'audit_result': audit_result,
                'evaluation': json.loads(saved['evaluation']) if saved['evaluation'] else None,
                'report': json.loads(saved['report']) if saved['report'] else None}

    def progress_assessment(self, job_id):
        job = self.job(job_id)
        spec = ResearchPlan.model_validate_json(job['spec'])
        run = self.db.execute('SELECT * FROM research_runs WHERE job_id=? ORDER BY created DESC LIMIT 1', (job_id,)).fetchone()
        if not run:
            return {'job_id': job_id, 'status': job['status'], 'semantic_progress_count': 0,
                    'semantic_progress_at': job['created'], 'suggested_action': 'continue', 'legacy_scope_only': not spec.evidence_obligations}
        workspace = self.db.execute('SELECT workspace FROM goals WHERE id=?', (job['goal_id'],)).fetchone()[0]
        tools = ResearchTools(self.store, run['id'], workspace, spec.scope, spec.budget.tool_calls,
                              obligations=[item.model_dump() for item in spec.evidence_obligations])
        progress = tools.obligation_progress()
        records = [(json.loads(row['detail']), row['created']) for row in self.db.execute(
            "SELECT detail,created FROM research_progress WHERE run_id=? AND kind='checkpoint' ORDER BY id", (run['id'],))]
        genuine = [(item, created) for item, created in records if item.get('semantic_progress')]
        # Reviewer calls share the ledger but never renew the original researcher's watchdog.
        cutoff = self.db.execute("SELECT created FROM activity WHERE kind='research_report_saved' AND json_extract(detail,'$.run_id')=? ORDER BY id LIMIT 1", (run['id'],)).fetchone()
        if cutoff:
            genuine = [(item, created) for item, created in genuine if created <= cutoff[0]]
        remaining = max(0, spec.budget.tool_calls - run['tool_calls'])
        return {'job_id': job_id, 'run_id': run['id'], 'status': job['status'], **progress,
                'report_saved': run['report'] is not None,
                'checkpoint_recorded': bool(records), 'remaining_tool_calls': remaining,
                'semantic_progress_at': genuine[-1][1] if genuine else run['created'],
                'semantic_progress_count': sum(len(item['newly_captured_keys']) for item, _ in genuine),
                'legacy_scope_only': not bool(tools.obligations),
                'blocked_reason': 'required evidence obligations remain uncaptured' if progress['unresolved_obligations'] else None,
                'suggested_action': 'report' if remaining == 0 else 'record_checkpoint' if progress['required_obligations_complete'] and not records else 'continue'}

    def save_context(self, run_id, role, context, revisions):
        self.store.execute('INSERT INTO research_run_contexts VALUES(?,?,?,?,?)',
            (run_id, role, json.dumps(context), json.dumps(revisions), time.time()))

    def portfolio_context(self, goal_id, workspace):
        """Bounded strategic context, explicitly separate from behavioral evidence."""
        from pathlib import Path
        from collections import Counter
        portfolio = {'audit_summary': {'status': 'unknown; no registered audit agenda'}, 'audit_agenda': [],
                     'opportunities': [], 'manifest': {'status': 'unknown; MANIFEST.json unavailable'},
                     'note': 'Inventory and portfolio context guide value judgments; they do not establish source behavior.'}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_areas'").fetchone():
            from .audit import Audit
            audit = Audit(self.store, goal_id)
            portfolio['audit_summary'] = audit.summary()
            portfolio['audit_agenda'] = [{**{k: row[k] for k in ('path','status','priority','job_id')},
                'rationale': row['rationale'][:400], 'next_question': row['next_question'][:600]}
                for row in audit.list()['areas']]
        portfolio['opportunities'] = [{'id': row['id'], 'title': row['title'], 'score': row['score'],
            'status': row['status'], 'actionable': row['spec'].get('actionable', True),
            'scope_gaps': [gap[:500] for gap in row['spec'].get('scope_gaps', [])[:3]]}
            for row in self.knowledge.backlog(goal_id)[:5]]
        try:
            _, data = read_source_bytes(workspace, 'MANIFEST.json')
            manifest = json.loads(data)
            if not isinstance(manifest, dict):
                raise ValueError('Manifest must be an object')
            metadata = {key: manifest[key] for key in ('repository','commit','scope','inventory_files',
                'exported_text_files','source_chunks','excluded_files','exclusions_detail') if key in manifest}
            exclusions = manifest.get('exclusions')
            if not isinstance(exclusions, list):
                detail = Path(self.store.path).parent / 'source-exclusions.json'
                if detail.is_file() and not detail.is_symlink():
                    with detail.open('rb') as stream:
                        raw = stream.read(1048577)
                    if len(raw) <= 1048576:
                        exclusions = json.loads(raw)
            if isinstance(exclusions, list):
                reasons = Counter(str(item.get('reason', 'unknown'))[:200] for item in exclusions if isinstance(item, dict))
                metadata['excluded_files'] = len(exclusions)
                metadata['exclusion_reasons'] = dict(reasons.most_common(20))
            else:
                metadata['exclusion_reasons'] = 'unknown; detailed exclusion ledger unavailable'
            metadata['status'] = 'captured export metadata; unaudited exclusions are not behavioral evidence'
            portfolio['manifest'] = metadata
        except (OSError, ValueError, TypeError) as exc:
            portfolio['manifest']['reason'] = str(exc)[:300]
        return portfolio

    def researcher_checkpoints(self, goal_id, run_id, before, evidence):
        available = {item['id']: item for item in evidence}
        records = []
        for row in self.db.execute("SELECT id,detail,created FROM activity WHERE goal_id=? AND kind='research_checkpoint' AND json_extract(detail,'$.run_id')=? AND created<=? ORDER BY id", (goal_id, run_id, before)):
            detail = json.loads(row['detail'])
            citations = detail.get('evidence_ids', [])
            valid = bool(citations) and all(source in available and not available[source]['stale'] for source in citations)
            records.append({'event_id': row['id'], 'created': row['created'], 'detail': detail, 'current_citations': valid})
        return records

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
        # Recheck actual files and current constraints immediately before any role charge.
        admission = self.preflight(job['goal_id'], job['stage'], spec)
        spec = admission['spec']
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
        review_budget_id = None
        try:
            from .budget import Budget, budget_enabled
            budget = Budget(self.store, job['goal_id']) if budget_enabled(self.store, job['goal_id']) else None
            if job['stage'] == 'research' and budget is not None and budget.economical_policy() is not None:
                budget.admit_review(run_id, spec.review_budget_usd)
                review_budget_id = run_id
            require_checkpoint = self.require_checkpoint or revision['research_context'].get('require_checkpoint',False)
            tools = ResearchTools(self.store, run_id, workspace, spec.scope, spec.budget.tool_calls,
                require_checkpoint=require_checkpoint, obligations=[item.model_dump() for item in spec.evidence_obligations],
                allow_scope_expansion=spec.allow_scope_expansion, dependency_roots=spec.dependency_roots,
                max_sources=8 if job['stage'] == 'research' else 20, deadline=time.time() + spec.budget.seconds)
            # Preflight credits declared current observations, so they must be
            # available independently of the bounded latest-knowledge prefetch.
            for obligation in spec.evidence_obligations:
                if obligation.locator in tools.scope:
                    for source_id in obligation.evidence_ids:
                        self.store.execute('INSERT OR IGNORE INTO research_access VALUES(?,?)', (run_id, source_id))
            # Mandatory retrieval happens before any new investigation, even if the model omits it.
            existing = tools.prefetch()
            context = {'plan': spec.model_dump(), 'prior_knowledge': existing, 'feedback': feedback,
                       'goal': self.store.snapshot(job['goal_id'])['goal'],
                       'research_context': revision['research_context'], 'policy': revision['policy']['instructions'],
                       'portfolio': self.portfolio_context(job['goal_id'], workspace),
                       'preflight': admission['proof'], 'review_budget_id': review_budget_id,
                       'process_requirements': {'researcher_checkpoint_before_report': require_checkpoint,
                           'required_evidence_obligations': bool(spec.evidence_obligations),
                           'typed_stop_reason': revision['research_context'].get('require_evidence_plan', False),
                           'reserved_checkpoint_calls': int(require_checkpoint)},
                       'scope_repair': {'authorized': spec.allow_scope_expansion, 'approved_roots': spec.dependency_roots,
                           'maximum_grants': 2, 'maximum_files_per_grant': 2, 'maximum_total_sources': tools.max_sources,
                           'instruction': 'Cite a captured callsite and exact prerequisite range; no budget or deadline reset.'}}
            self.save_context(run_id, job['stage'], context, revision)
            report = await asyncio.wait_for(self.agents.run(job['stage'], context, tools), spec.budget.seconds)
            report = (DiscoveryReport if job['stage'] == 'discovery' else ResearchReport).model_validate(report)
            # Persist completed work before evaluation, revision checks, or another budget charge.
            report_saved_at = time.time()
            report_progress = tools.obligation_progress()
            self.store.execute('INSERT INTO research_progress(run_id,kind,detail,created) VALUES(?,?,?,?)',
                (run_id, 'report_snapshot', json.dumps(report_progress), report_saved_at))
            self.store.execute('UPDATE research_runs SET report=? WHERE id=?', (report.model_dump_json(), run_id))
            Control(self.store).event(job['goal_id'], 'research_report_saved', {'run_id': run_id, 'saved_at': report_saved_at})
            if Control(self.store).current(job['goal_id']) != revision or self.job(job_id)['status'] == 'invalidated':
                return self.finish(job, run_id, 'stale', feedback='Goal or policy changed during research; retained report is not accepted evidence')
            if job['stage'] == 'discovery':
                report = DiscoveryReport.model_validate(report)
                self.save_discovery(job, report, tools)
                return self.finish(job, run_id, 'discovered', report)
            report = ResearchReport.model_validate(report)
            # Evaluation has a fresh agent context, captured excerpts, and its own bounded reads.
            self.charge(job['goal_id'])
            final_scope = sorted(tools.scope)
            context['process_requirements']['required_evidence_obligations'] = bool(tools.obligations)
            final_plan = ResearchPlan.model_validate({**spec.model_dump(), 'scope': final_scope, 'evidence_obligations': tools.obligations})
            review_tools = ResearchTools(self.store, run_id, workspace, final_scope, spec.budget.tool_calls,
                obligations=[item.model_dump() for item in spec.evidence_obligations])
            evidence = tools.evidence()
            checkpoints = self.researcher_checkpoints(job['goal_id'], run_id, report_saved_at, evidence)
            process_attestation = {'checkpoint_before_report': any(item['current_citations'] for item in checkpoints),
                'researcher_checkpoint_ids': [item['event_id'] for item in checkpoints if item['current_citations']],
                'required_obligations_complete': report_progress['required_obligations_complete'],
                'report_saved_at': report_saved_at, 'source': 'Python evidence ledger snapshot before evaluator launch'}
            review_context = {'plan': final_plan.model_dump(), 'report': report.model_dump(), 'evidence': evidence,
                'goal': context['goal'], 'research_context': revision['research_context'], 'policy': context['policy'],
                'portfolio': context['portfolio'], 'process_requirements': context['process_requirements'],
                'researcher_checkpoints': checkpoints, 'process_attestation': process_attestation,
                'final_scope': final_scope, 'scope_grants': tools.grants(),
                'evidence_obligation_progress': report_progress, 'review_budget_id': review_budget_id}
            self.save_context(run_id, 'evaluation', review_context, revision)
            evaluation = await asyncio.wait_for(self.agents.run('evaluation', review_context, review_tools), spec.budget.seconds)
            evaluation = Evaluation.model_validate(evaluation)
            self.store.execute('UPDATE research_runs SET evaluation=? WHERE id=?', (evaluation.model_dump_json(), run_id))
            if Control(self.store).current(job['goal_id']) != revision or self.job(job_id)['status'] == 'invalidated':
                return self.finish(job, run_id, 'stale', report, feedback='Revision changed during evaluation')
            evaluation = self.gate(report, evaluation, review_tools.evidence(), revision['research_context']['rubric'],
                context['process_requirements'], process_attestation)
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
        finally:
            if review_budget_id is not None:
                budget.release_review(review_budget_id)

    async def reevaluate(self, job_id, feedback, max_reviews=1):
        """One explicit independent review of retained work, never a research replay."""
        from .control import Control
        if type(max_reviews) is not int or max_reviews != 1:
            raise ValueError('A research job permits at most one additional review; this limit cannot be reset')
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000:
            raise ValueError('Reevaluation requires explicit targeted feedback of 1..4000 characters')
        job = self.job(job_id)
        if job['stage'] != 'research' or job['status'] in ('pending','running','supported','falsified','discovered','invalidated','stale'):
            raise ValueError('Only completed, unaccepted research with a retained report can be reevaluated')
        if self.db.execute('SELECT 1 FROM research_reviews WHERE job_id=?', (job_id,)).fetchone():
            raise ValueError('Additional review budget exhausted; retain the result or plan distinct fresh research')
        run = self.db.execute('SELECT * FROM research_runs WHERE job_id=? AND report IS NOT NULL AND finished IS NOT NULL ORDER BY created DESC LIMIT 1', (job_id,)).fetchone()
        if run is None:
            raise ValueError('No completed persisted research report is available')
        saved = self.db.execute("SELECT context,revisions FROM research_run_contexts WHERE run_id=? AND role='research'", (run['id'],)).fetchone()
        current = Control(self.store).current(job['goal_id'])
        if saved is None or json.loads(saved['revisions']) != current:
            raise ValueError('Goal, context or policy revisions differ from the original research; fresh research is required')
        report = ResearchReport.model_validate_json(run['report'])
        spec = ResearchPlan.model_validate_json(job['spec'])
        workspace = self.store.snapshot(job['goal_id'])['goal']['workspace']
        tools = ResearchTools(self.store, run['id'], workspace, spec.scope, spec.budget.tool_calls,
            obligations=[item.model_dump() for item in spec.evidence_obligations])
        evidence = tools.evidence()
        available = {item['id']: item for item in evidence}
        cited = set(report.supporting_evidence + report.contradictory_evidence)
        cited.update(source for finding in report.findings for source in finding.evidence_ids)
        if any(source not in available or available[source]['stale'] for source in cited):
            raise ValueError('Retained report cites unknown or stale evidence; fresh research is required')
        event = self.db.execute("SELECT detail FROM activity WHERE goal_id=? AND kind='research_report_saved' AND json_extract(detail,'$.run_id')=? ORDER BY id LIMIT 1", (job['goal_id'], run['id'])).fetchone()
        prior_review = self.db.execute("SELECT context,created FROM research_run_contexts WHERE run_id=? AND role='evaluation'", (run['id'],)).fetchone()
        cutoff = json.loads(event['detail'])['saved_at'] if event else None
        if cutoff is None and prior_review:
            cutoff = json.loads(prior_review['context']).get('process_attestation', {}).get('report_saved_at', prior_review['created'])
        checkpoints = self.researcher_checkpoints(job['goal_id'], run['id'], cutoff, evidence) if cutoff is not None else []
        attestation = {'checkpoint_before_report': any(item['current_citations'] for item in checkpoints),
            'researcher_checkpoint_ids': [item['event_id'] for item in checkpoints if item['current_citations']],
            'report_saved_at': cutoff, 'source': 'Original report cutoff; evaluator events cannot satisfy researcher requirements'}
        snapshot = self.db.execute("SELECT detail FROM research_progress WHERE run_id=? AND kind='report_snapshot' ORDER BY id LIMIT 1", (run['id'],)).fetchone()
        original_progress = json.loads(snapshot[0]) if snapshot else tools.obligation_progress([]) if tools.obligations else tools.obligation_progress()
        attestation['required_obligations_complete'] = original_progress['required_obligations_complete']
        requirements = dict(json.loads(saved['context']).get('process_requirements', {}))
        requirements['researcher_checkpoint_before_report'] = self.require_checkpoint or bool(requirements.get('researcher_checkpoint_before_report'))
        if prior_review:
            requirements['researcher_checkpoint_before_report'] |= bool(json.loads(prior_review['context']).get('process_requirements', {}).get('researcher_checkpoint_before_report'))
        requirements['required_evidence_obligations'] = bool(tools.obligations)
        from .budget import Budget, budget_enabled
        review_id = uuid.uuid4().hex
        budget = Budget(self.store, job['goal_id']) if budget_enabled(self.store, job['goal_id']) else None
        escrow_id = review_id if budget is not None and budget.economical_policy() is not None else None
        final_plan = ResearchPlan.model_validate({**spec.model_dump(), 'scope': sorted(tools.scope), 'evidence_obligations': tools.obligations})
        context = {'plan': final_plan.model_dump(), 'report': report.model_dump(), 'evidence': evidence,
            'goal': self.store.snapshot(job['goal_id'])['goal'], 'research_context': current['research_context'],
            'policy': current['policy']['instructions'], 'portfolio': self.portfolio_context(job['goal_id'], workspace),
            'researcher_checkpoints': checkpoints, 'process_requirements': requirements, 'process_attestation': attestation,
            'final_scope': sorted(tools.scope), 'scope_grants': tools.grants(), 'evidence_obligation_progress': original_progress,
            'review_budget_id': escrow_id,
            'review_feedback': feedback, 'review_kind': 'One additional evaluation of the original persisted report; no research replay'}
        # The unique job constraint and role charge commit together before any model call.
        with self.db:
            self.db.execute('INSERT INTO research_reviews(id,job_id,run_id,feedback,context,revisions,status,created) VALUES(?,?,?,?,?,?,?,?)',
                (review_id, job_id, run['id'], feedback, json.dumps(context), json.dumps(current), 'running', time.time()))
            self.charge(job['goal_id'])

        def finish_review(status, evaluation=None, error=None):
            self.store.execute('UPDATE research_reviews SET status=?,evaluation=?,error=?,finished=? WHERE id=? AND status=?',
                (status, evaluation.model_dump_json() if evaluation else None, error, time.time(), review_id, 'running'))
            return {'review_id': review_id, 'job_id': job_id, 'run_id': run['id'], 'status': status,
                'report': report.model_dump(), 'evaluation': evaluation.model_dump() if evaluation else None,
                'feedback': error or (evaluation.feedback if evaluation else '')}

        try:
            if escrow_id:
                budget.admit_review(escrow_id, spec.review_budget_usd)
            evaluation = Evaluation.model_validate(await asyncio.wait_for(self.agents.run('evaluation', context, tools), spec.budget.seconds))
            if Control(self.store).current(job['goal_id']) != current or self.job(job_id)['status'] in ('stale','invalidated'):
                return finish_review('stale', evaluation, 'Revision changed during additional evaluation; result is not accepted')
            evaluation = self.gate(report, evaluation, tools.evidence(), current['research_context']['rubric'], requirements, attestation)
            self.save_findings(job, report, evaluation)
            if evaluation.verdict in ('supported','falsified'):
                with self.db:
                    self.db.execute('UPDATE research_jobs SET status=?,updated=? WHERE id=?', (evaluation.verdict, time.time(), job_id))
                    if spec.opportunity_id:
                        self.db.execute('UPDATE opportunities SET status=?,updated=? WHERE id=?', (evaluation.verdict, time.time(), spec.opportunity_id))
            result = finish_review(evaluation.verdict, evaluation)
            if evaluation.verdict in ('supported','falsified'):
                result['audit_result'] = self.record_audit_result(job)
            return result
        except asyncio.CancelledError:
            finish_review('interrupted', error='Additional review cancelled; its budget remains spent and its original report is retained')
            raise
        except Exception as exc:
            return finish_review('blocked', error=f'{type(exc).__name__}: {exc}')
        finally:
            if escrow_id:
                budget.release_review(escrow_id)

    @staticmethod
    def gate(report, evaluation, evidence, rubric=(), process_requirements=None, process_attestation=None):
        available = {e['id']: e for e in evidence}
        cited = set(report.supporting_evidence + report.contradictory_evidence + evaluation.evidence_ids)
        claims = [f for f in report.findings if f.classification == 'claim']
        cited.update(s for finding in report.findings for s in finding.evidence_ids)
        errors = []
        if any(s not in available or available[s]['stale'] for s in cited):
            errors.append('Unknown or stale evidence IDs; read current scoped sources')
        if (evaluation.verdict in ('supported', 'falsified') or evaluation.evidential_verdict in ('supported', 'falsified')) and not evaluation.evidence_ids:
            errors.append('Evaluator must cite independently available evidence')
        if (evaluation.verdict == 'supported' or evaluation.evidential_verdict == 'supported') and (not claims or any(not f.evidence_ids for f in claims)):
            errors.append('Every asserted claim needs observed evidence; assumptions are not verified facts')
        integrity_errors = list(errors)
        if report.blocked and evaluation.verdict == 'supported':
            errors.append('Blocked research cannot establish support')
        if rubric and evaluation.verdict == 'supported':
            assessments = evaluation.rubric_assessments
            if len(assessments) != len(rubric) or {a.criterion for a in assessments} != set(rubric):
                errors.append('Evaluator must assess every exact rubric criterion once')
            for assessment in assessments:
                if assessment.verdict == 'unmet':
                    errors.append('Unmet rubric criterion: ' + assessment.criterion)
                if assessment.verdict == 'met' and not assessment.evidence_ids:
                    errors.append('Met rubric criteria require evidence')
                if any(s not in available or available[s]['stale'] for s in assessment.evidence_ids):
                    errors.append('Rubric assessment cites unknown or stale evidence')
                    integrity_errors.append('Rubric assessment cites unknown or stale evidence')
        checkpoint_missing = (process_requirements or {}).get('researcher_checkpoint_before_report') and not (process_attestation or {}).get('checkpoint_before_report')
        if checkpoint_missing:
            errors.append('Required researcher checkpoint with current evidence was not recorded before the report')
        if (process_requirements or {}).get('required_evidence_obligations') and not (process_attestation or {}).get('required_obligations_complete'):
            errors.append('Required evidence obligations were not completely captured before the report')
        if report.stop_reason in ('incomplete_scope','access_unavailable','tool_budget','time_budget') and evaluation.verdict == 'supported':
            errors.append('Research stopped with unresolved prerequisites or resource limits')
        if (process_requirements or {}).get('typed_stop_reason') and report.stop_reason is None:
            errors.append('Evidence-plan research requires an explicit typed stop_reason')
        if errors:
            return evaluation.model_copy(update={'verdict': 'insufficient',
                'evidential_verdict': 'insufficient' if integrity_errors else evaluation.evidential_verdict,
                'evidence_ids': [] if integrity_errors else evaluation.evidence_ids,
                'workflow_issues': list(dict.fromkeys(evaluation.workflow_issues + errors))[:20],
                'reason': '; '.join(errors)[:3000], 'feedback': '; '.join(errors)[:3000]})
        return evaluation

    def save_discovery(self, job, report, tools):
        available = {s['id']: s for s in tools.evidence()}
        workspace = self.store.snapshot(job['goal_id'])['goal']['workspace']
        for entry in report.system_map:
            citations = [s for s in entry.evidence_ids if s in available and not available[s]['stale']]
            # Discovery is reconnaissance, not independent verification.
            self.knowledge.put(job['goal_id'], job['id'], 'system_map', entry.name,
                entry.model_dump(), 'assumption' if entry.assumption or not citations else 'observed', citations)
        for opportunity in report.opportunities:
            citations = [s for s in opportunity.evidence_ids if s in available and not available[s]['stale']]
            scope_gaps = list(opportunity.scope_gaps)
            if not opportunity.actionable and not scope_gaps:
                scope_gaps.append('Discovery identified an unresolved scope gap; resolve explicit implementation and test locators')
            for locator in opportunity.scope:
                try:
                    _, data = read_source_bytes(workspace, locator)
                    data.decode('utf-8')
                except (OSError, ValueError) as exc:
                    scope_gaps.append(f'{locator}: {exc}; identify an existing bounded UTF-8 source file using navigation')
            actionable = opportunity.actionable and not scope_gaps
            confidence = opportunity.confidence if citations and actionable else 0
            score = opportunity.impact * confidence / (opportunity.risk * opportunity.effort)
            opportunity = opportunity.model_copy(update={'confidence': confidence, 'evidence_ids': citations,
                'actionable': actionable, 'scope_gaps': scope_gaps[:20]})
            # Repeated discovery should update the backlog rather than multiply entries.
            prior = self.db.execute('SELECT id,status FROM opportunities WHERE goal_id=? AND title=?',
                                    (job['goal_id'], opportunity.title)).fetchone()
            status = 'needs_scope' if not actionable else (prior['status'] if prior and prior['status'] != 'needs_scope' else 'open')
            if not prior:
                now = time.time()
                self.store.execute('INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, job['goal_id'], job['id'], opportunity.title,
                     opportunity.model_dump_json(), score, status, now, now))
            else:
                self.store.execute('UPDATE opportunities SET spec=?,score=?,status=?,updated=? WHERE id=?',
                    (opportunity.model_dump_json(),score,status,time.time(),prior['id']))
        self.knowledge.put(job['goal_id'], job['id'], 'discovery_gaps', 'Discovery gaps',
                           report.gaps, 'assumption', [])

    def save_findings(self, job, report, evaluation):
        run = self.db.execute('SELECT id FROM research_runs WHERE job_id=? ORDER BY created DESC LIMIT 1', (job['id'],)).fetchone()
        available = {row[0] for row in self.db.execute('SELECT source_id FROM research_access WHERE run_id=?', (run[0],))} if run else set()
        cited = set(report.supporting_evidence + report.contradictory_evidence + evaluation.evidence_ids)
        cited.update(source for finding in report.findings for source in finding.evidence_ids)
        cited.update(source for assessment in evaluation.rubric_assessments for source in assessment.evidence_ids)
        intact = bool(cited) and cited.issubset(available) and all(not self.knowledge.source(source)['stale'] for source in cited)
        for finding in report.findings:
            independently_cited = bool(finding.evidence_ids) and set(finding.evidence_ids).issubset(evaluation.evidence_ids) and finding.classification == 'claim'
            classification = 'verified' if evaluation.verdict == 'supported' and independently_cited and intact else 'assumption'
            if evaluation.verdict != 'supported' and evaluation.evidential_verdict == 'supported' and independently_cited and intact:
                classification = 'supported_observation'
            self.knowledge.put(job['goal_id'], job['id'], 'finding', finding.statement,
                {'finding': finding.model_dump(), 'evaluation': evaluation.model_dump(),
                 'uncertainties': report.uncertainties}, classification,
                [s for s in finding.evidence_ids if self.db.execute('SELECT 1 FROM research_sources WHERE id=?', (s,)).fetchone()])
        if evaluation.verdict == 'falsified':
            self.knowledge.put(job['goal_id'], job['id'], 'rejected_hypothesis', json.loads(job['spec'])['hypothesis'],
                {'reason': evaluation.reason, 'uncertainties': report.uncertainties}, 'rejected', evaluation.evidence_ids)
