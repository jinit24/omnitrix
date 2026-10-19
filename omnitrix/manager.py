import fcntl
import json
import os
from agents import Agent, Runner, RunConfig, function_tool
from .control import Control
from .knowledge import Knowledge
from .research import Research
from .implementation import Implementations
from .execution import Execution
from .models import Decision
from .workers import ROOT
from .budget import budget_run_config, close_budget_run_config, assert_no_unmetered_worker


from .manager_context import (manager_context, merge_manager_history, manager_history_filter,
                              bounded_json, card, message_card, clip)

class AgentBrain:
    """A conversational Agents SDK agent with durable context and recall tools."""
    def __init__(self, model=None, store=None):
        self.store = store
        self.model = model or os.getenv('OMNITRIX_MODEL', 'gpt-4.1')

    async def decide(self, snapshot):
        @function_tool
        async def recall_history(section: str, offset: int = 0) -> str:
            """Read up to ten compact history cards with exact IDs; retrieve_record/read_report provide full paged detail."""
            if offset<0:
                return 'Use a nonnegative offset.'
            if section == 'research' and self.store:
                rows = [dict(r) for r in self.store.db.execute('SELECT id,stage,status,attempts FROM research_jobs WHERE goal_id=? ORDER BY created DESC LIMIT 10 OFFSET ?', (snapshot['goal']['id'],offset))]
                return bounded_json(rows)
            if section=='messages' and self.store:
                return bounded_json([message_card(dict(r)) for r in self.store.db.execute('SELECT id,kind,body,direction,created FROM messages WHERE goal_id=? ORDER BY created LIMIT 10 OFFSET ?',(snapshot['goal']['id'],offset))])
            if section not in ('tasks', 'decisions'):
                return 'Use tasks, decisions, messages, or research.'
            if self.store:
                rows = [dict(r) for r in self.store.db.execute(f'SELECT * FROM {section} WHERE goal_id=? ORDER BY rowid LIMIT 10 OFFSET ?',(snapshot['goal']['id'],offset))]
                if section == 'tasks':
                    rows = [card(row,('id','status','adapter','instructions','error')) for row in rows]
                else:
                    rows = [{'id':row['id'],**card(json.loads(row['body']),('action','rationale','research_id','implementation_id','user_message'))} for row in rows]
                return bounded_json(rows)
            return bounded_json([card(item,('id','action','status','rationale','error')) for item in snapshot[section][offset:offset + 10]])

        tools = [recall_history]
        policy = ''
        session = None
        if self.store:
            control = Control(self.store)
            goal_id = snapshot['goal']['id']
            current = control.current(goal_id)
            policy = '\nDurable manager policy:\n' + current['policy']['instructions']
            session = control.session(goal_id)
            @function_tool
            async def search_knowledge(query: str = '', offset: int = 0) -> str:
                """Retrieve shared verified facts, system maps, assumptions and rejected hypotheses."""
                return bounded_json(Knowledge(self.store).search(snapshot['goal']['workspace'], query, offset=offset))

            @function_tool
            async def retrieve_record(section: str, record_id: str, offset: int = 0) -> str:
                """Read 1500 UTF-8 bytes of a durable task, decision, research or message by exact ID. Follow next_offset for full detail."""
                if offset < 0:
                    return 'Use a nonnegative byte offset.'
                tables = {'tasks':'tasks','decisions':'decisions','research':'research_jobs','messages':'messages'}
                table = tables.get(section)
                if table is None:
                    return 'Use tasks, decisions, research, or messages.'
                row = self.store.db.execute(f'SELECT * FROM {table} WHERE id=? AND goal_id=?',(record_id,goal_id)).fetchone()
                if row is None:
                    return 'No matching record in this goal.'
                raw = json.dumps(dict(row),ensure_ascii=False).encode('utf-8')
                if offset > len(raw) or offset < len(raw) and raw[offset] & 0xC0 == 0x80:
                    return 'Use offset zero or the exact next_offset returned by the previous page.'
                chunk = raw[offset:offset+1500]
                # Back up to a valid UTF-8 boundary so paging never skips bytes.
                while chunk:
                    try:
                        text = chunk.decode('utf-8')
                        break
                    except UnicodeDecodeError:
                        chunk = chunk[:-1]
                else:
                    text = ''
                result = {'section':section,'id':record_id,'offset':offset,'text':text,
                          'next_offset':offset+len(chunk) if offset+len(chunk)<len(raw) else None,'total_bytes':len(raw)}
                control.event(goal_id,'record_retrieved',{'section':section,'id':record_id,'offset':offset,'bytes':len(chunk)})
                return bounded_json(result)

            @function_tool
            async def read_report(job_id: str, section: str = 'report', offset: int = 0) -> str:
                """Page a saved research report, original evaluation or additional review. Returns 1500 bytes and next_offset; does not repeat research."""
                if offset < 0 or section not in ('report','evaluation','review'):
                    return 'Use report, evaluation or review with a nonnegative byte offset.'
                if section == 'review':
                    row = self.store.db.execute('''SELECT r.id,r.run_id,r.status,r.evaluation,r.feedback,r.error FROM research_reviews r
                        JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=? AND j.id=? ORDER BY r.created DESC LIMIT 1''',(goal_id,job_id)).fetchone()
                else:
                    row = self.store.db.execute(f'''SELECT r.id,r.status,r.{section} AS content,r.feedback FROM research_runs r
                        JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=? AND j.id=? AND r.{section} IS NOT NULL
                        ORDER BY r.created DESC LIMIT 1''',(goal_id,job_id)).fetchone()
                if row is None:
                    return 'No saved section for this research job in this goal.'
                raw = json.dumps(dict(row),ensure_ascii=False).encode('utf-8')
                if offset > len(raw) or offset < len(raw) and raw[offset] & 0xC0 == 0x80:
                    return 'Use offset zero or the exact next_offset returned by the previous page.'
                chunk = raw[offset:offset+1500]
                while chunk:
                    try:
                        text = chunk.decode('utf-8')
                        break
                    except UnicodeDecodeError:
                        chunk = chunk[:-1]
                else:
                    text = ''
                result = {'job_id':job_id,'section':section,'offset':offset,'text':text,
                    'total_bytes':len(raw),'next_offset':offset+len(chunk) if offset+len(chunk)<len(raw) else None}
                control.event(goal_id,'report_retrieved',{'job_id':job_id,'section':section,'offset':offset,'bytes':len(chunk)})
                return bounded_json(result)
            @function_tool
            async def list_resources(directory: str = '.', offset: int = 0, query: str = '') -> str:
                """List 40 sorted children with next_offset; optionally filter names by literal query."""
                from .navigation import list_resources as navigate
                try:
                    result = navigate(snapshot['goal']['workspace'], directory, offset, query)
                except ValueError as exc:
                    return str(exc)
                control.event(goal_id, 'resource_listing', {'directory':directory,'offset':offset,'query':query,'result':result})
                return bounded_json(result)
            @function_tool
            async def find_resources(query: str = '', offset: int = 0) -> str:
                """Find matching file paths across the workspace, 40 per page; literal substring query."""
                from .navigation import find_resources as find
                result = find(snapshot['goal']['workspace'], query, offset)
                control.event(goal_id, 'resource_search', {'query':query,'offset':offset,'result':result})
                return bounded_json(result)

            @function_tool
            async def search_sources(query: str, directory: str = 'sources', offset: int = 0) -> str:
                """Search literal text across source files for scope selection. Matches are navigation, not verified evidence."""
                from .navigation import search_workspace
                result = search_workspace(snapshot['goal']['workspace'], query, directory, offset)
                control.event(goal_id, 'source_search', {'query':query,'directory':directory,'offset':offset,'result':result})
                return bounded_json(result)

            @function_tool
            async def audit_agenda(status: str = '', offset: int = 0, query: str = '') -> str:
                """Retrieve persistent whole-repository audit areas, priorities, unanswered questions and coverage gaps."""
                from .audit import Audit
                return bounded_json(Audit(self.store,goal_id).list(status,offset,query))

            @function_tool
            async def update_audit(area: str, status: str, priority: int, rationale: str, next_question: str, job_id: str | None = None) -> str:
                """Record strategic area triage and next question. Sampled needs an independently evaluated job; it never means fully audited."""
                from .audit import Audit
                return bounded_json(Audit(self.store,goal_id).update(area,status,priority,rationale,next_question,job_id))

            @function_tool
            async def read_catalog(locator: str, start_line: int = 1, line_count: int = 80) -> str:
                """Read bounded workspace catalog/manifest metadata directly to select exact source paths; not behavior evidence."""
                from .knowledge import read_source_bytes
                from pathlib import PurePosixPath
                canonical = str(PurePosixPath(locator))
                if canonical not in ('MANIFEST.json','CATALOG.md') and not canonical.startswith('catalog/'):
                    return 'Read catalog/ metadata or MANIFEST.json; use typed research for source evidence'
                if start_line < 1 or not 1 <= line_count <= 100:
                    return 'Read 1..100 lines starting at line 1 or later'
                _, data = read_source_bytes(snapshot['goal']['workspace'], canonical)
                lines = data.decode('utf-8').splitlines()
                excerpt = '\n'.join(f'{i+1}: {line}' for i,line in enumerate(lines) if start_line <= i+1 < start_line+line_count)[:12000]
                result = {'locator':canonical,'excerpt':excerpt,'total_lines':len(lines),'navigation_only':True}
                control.event(goal_id,'catalog_read',result)
                return bounded_json(result)

            @function_tool
            async def expand_audit(area: str, new_area: str, directory: str) -> str:
                """Split a large audit area into a persistent child frontier using actual source-directory metadata; no coverage is implied."""
                from .audit import Audit
                return bounded_json(Audit(self.store,goal_id).expand(area,new_area,directory))

            tools += [search_knowledge, list_resources, find_resources, search_sources, audit_agenda, update_audit, read_catalog, expand_audit, retrieve_record, read_report]
        agent = Agent(
            name='Engineering manager', model=self.model,
            instructions=(ROOT / 'instructions/MANAGER.md').read_text() + '\n' +
                         (ROOT / 'instructions/KNOWLEDGE.md').read_text() + policy,
            tools=tools, output_type=Decision)
        context = manager_context(snapshot)
        run_config = budget_run_config(self.store, snapshot['goal']['id'], 'manager', model=self.model)
        run_config.session_input_callback = (lambda history,new_input: merge_manager_history([],new_input)) if snapshot.get('manager_repair',{}).get('fresh_context') else merge_manager_history
        run_config.call_model_input_filter = manager_history_filter
        turn_limit = 20 if snapshot.get('audit_coverage') else 8
        try:
            result = await Runner.run(agent, json.dumps(context), max_turns=turn_limit, session=session,
                                      run_config=run_config)
            if self.store:
                from .telemetry import record_usage
                record_usage(self.store,snapshot['goal']['id'],result,'manager')
            return result.final_output
        finally:
            await close_budget_run_config(run_config)
            if session:
                session.close()


class Manager:
    def __init__(self, store, brain, worker):
        self.store, self.brain, self.worker = store, brain, worker

    async def resume(self, goal_id, steps=5, message=None):
        if steps < 1:
            raise ValueError('steps must be positive')
        # One owner per database, including separate CLI processes. A crash releases it.
        with open(str(self.store.path) + '.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another manager owns this database') from None
            return await self._resume(goal_id, steps, message)

    async def _resume(self, goal_id, steps, message):
        snapshot = self.store.snapshot(goal_id)
        if snapshot['goal']['status'] == 'completed':
            return snapshot
        # No other manager can own the database while this lock is held.
        self.store.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
        self.store.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
        if message:
            self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                               (goal_id, json.dumps({'user_message': message})))
        self.store.execute("UPDATE goals SET status='active' WHERE id=?", (goal_id,))
        for _ in range(steps):
            from .campaign import Campaign
            campaign = Campaign(self.store,goal_id)
            queue = campaign.summary()['queue']
            if queue['claimed'] or queue['uncertain'] or queue['interrupted']:
                Control(self.store).event(goal_id,'campaign_reconciliation_required',{'queue':queue})
                self.store.execute("UPDATE goals SET status='paused' WHERE id=?",(goal_id,))
                break
            claimed = campaign.claim_next()
            if claimed:
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',(goal_id,json.dumps({
                    'action':'run_queued_research','queue_id':claimed['id'],'research_id':claimed['job_id'],
                    'rationale':'Continue a manager-approved revision-current bounded research plan without another model call'})))
                try:
                    outcome = await Research(self.store).attempt(claimed['job_id'],claimed['instructions'])
                except BaseException:
                    campaign.complete_job(claimed['job_id'],{'job_id':claimed['job_id'],'status':'uncertain'})
                    raise
                campaign.complete_job(claimed['job_id'],outcome)
                continue
            decision = await self.brain.decide(self.store.snapshot(goal_id))
            if decision.action == 'schedule_research':
                from .scheduling import enqueue_batch
                outcome = enqueue_batch(self.store,goal_id,decision.research_batch,instructions=decision.instructions)
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                    (goal_id,json.dumps({'decision':decision.model_dump(),'scheduled':outcome})))
                continue
            if decision.action == 'reevaluate':
                research = Research(self.store)
                job = research.job(decision.research_id)
                if job['goal_id'] != goal_id:
                    raise ValueError('Research job belongs to another goal')
                await research.reevaluate(job['id'],decision.instructions)
                continue
            if decision.action in ('discover', 'research', 'retry_research'):
                research = Research(self.store)
                if decision.action == 'retry_research':
                    job = research.job(decision.research_id)
                    if job['goal_id'] != goal_id:
                        raise ValueError('Research job belongs to another goal')
                    outcome = await research.attempt(job['id'], decision.instructions)
                else:
                    if decision.research_plan is None:
                        raise ValueError('Research action requires a bounded plan')
                    planned = research.plan(goal_id, 'discovery' if decision.action == 'discover' else 'research', decision.research_plan)
                    outcome = planned if planned['duplicate'] else await research.attempt(planned['job_id'], decision.instructions)
                self.store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                    (goal_id, json.dumps({'decision': decision.model_dump(), 'research_outcome': outcome})))
                continue
            if decision.action in ('implement', 'audit'):
                if decision.action == 'implement':
                    assert_no_unmetered_worker(self.store, goal_id)
                service = Implementations(self.store)
                row = service.get(decision.implementation_id)
                if row['goal_id'] != goal_id:
                    raise ValueError('Implementation belongs to another goal')
                if decision.action == 'implement':
                    await service.run(row['id'])
                else:
                    service.audit(row['id'])
                continue
            if decision.action == 'complete' and self.store.snapshot(goal_id).get('audit_coverage'):
                from .audit import Audit
                if Audit(self.store,goal_id).completion_gaps():
                    raise ValueError('Audit remains incomplete; triaged and deferred areas still need independent source sampling')
            # Validate references before recording any executable assignment.
            task = None
            if decision.action == 'follow_up':
                task = self.store.task(decision.task_id)
                if task['goal_id'] != goal_id or task['adapter'] != self.worker.name:
                    raise ValueError('Follow-up must belong to this goal and adapter')
            if decision.action in ('delegate', 'follow_up') and not decision.instructions.strip():
                raise ValueError('Delegation requires bounded instructions')
            if decision.action in ('delegate', 'follow_up'):
                assert_no_unmetered_worker(self.store, goal_id)
            with self.store.db:
                self.store.db.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
                                      (goal_id, decision.model_dump_json()))
                if decision.action in ('complete', 'pause'):
                    if decision.action=='complete' and (self.store.snapshot(goal_id)['implementations'] or self.store.snapshot(goal_id)['goal']['requires_verified_implementation']):
                        current = Control(self.store).current(goal_id)
                        if not self.store.db.execute('SELECT 1 FROM completion_audits WHERE goal_id=? AND revision=? AND passed=1',(goal_id,current['goal']['revision'])).fetchone():
                            raise ValueError('Completion requires a current exact-revision goal audit')
                    status = 'completed' if decision.action == 'complete' else 'paused'
                    self.store.db.execute('UPDATE goals SET status=? WHERE id=?', (status, goal_id))
                    break
                execution = Execution(self.store, self.worker)
                if task is None:
                    task_id = execution.assign(goal_id, decision.instructions, decision.priority)
                else:
                    task_id = task['id']
            try:
                await execution.send(task_id, decision.instructions, snapshot['goal']['workspace'])
            except Exception:
                # The next agent turn sees the persisted failure and decides what to do.
                pass
        return self.store.snapshot(goal_id)
