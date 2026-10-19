"""Explicitly authorized Codex changes and executable, revision-bound verification."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid
from pydantic import Field
from .research_models import Contract
from .control import Control
from .knowledge import Knowledge
from .runtime import CodexRuntime

SCHEMA = '''
CREATE TABLE IF NOT EXISTS implementation_authorizations (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), goal_revision INTEGER NOT NULL,
 policy_revision INTEGER NOT NULL, spec TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS implementations (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), authorization TEXT NOT NULL,
 base_revision TEXT NOT NULL, candidate TEXT, worktree TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL, feedback TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS verifications (
 id TEXT PRIMARY KEY, implementation_id TEXT NOT NULL REFERENCES implementations(id),
 candidate TEXT NOT NULL, goal_revision INTEGER NOT NULL, requirement TEXT NOT NULL,
 argv TEXT NOT NULL, passed INTEGER NOT NULL, exit_code INTEGER,
 artifact TEXT NOT NULL, digest TEXT NOT NULL, failure_kind TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS external_actions (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), kind TEXT NOT NULL,
 target TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created REAL NOT NULL);
'''


class AcceptanceCheck(Contract):
    requirement: str = Field(min_length=1, max_length=2000)
    argv: list[str] = Field(min_length=1, max_length=30)
    timeout: int = Field(default=60, ge=1, le=600)
    failure_kind: str = Field(default='local', pattern='^(local|assumption)$')
    stdout_contains: str | None = None
    oracle_revisions: dict[str,str] = Field(default_factory=dict)


class ImplementationHandoff(Contract):
    summary: str = Field(max_length=4000)
    artifacts: list[str] = Field(max_length=20)
    tests_run: list[str] = Field(max_length=20)
    unresolved_concerns: list[str] = Field(max_length=20)
    questions: list[str] = Field(max_length=10)


class ImplementationSpec(Contract):
    objective: str = Field(min_length=10, max_length=6000)
    research_job: str
    allowed_paths: list[str] = Field(min_length=1, max_length=30)
    checks: list[AcceptanceCheck] = Field(min_length=1, max_length=30)
    max_attempts: int = Field(default=2, ge=1, le=3)
    seconds: int = Field(default=300, ge=1, le=1800)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.STDOUT).strip()


class Implementations:
    def __init__(self, store, runtime=None):
        self.store, self.db = store, store.db
        self.control = Control(store)
        self.runtime = runtime or CodexRuntime()

    def authorize(self, goal_id, spec, explicit=False):
        if not explicit:
            raise ValueError('Code modification requires explicit user authorization')
        current = self.control.current(goal_id)
        research = self.db.execute('SELECT * FROM research_jobs WHERE id=? AND goal_id=?',
                                   (spec.research_job, goal_id)).fetchone()
        if research is None or research['status'] != 'supported':
            raise ValueError('Implementation requires independently accepted research for this goal')
        required = set(current['goal']['requirements'])
        if not required.issubset({c.requirement for c in spec.checks}):
            raise ValueError('Every mandatory requirement needs an authorized executable check')
        for path in spec.allowed_paths:
            p = Path(path)
            if p.is_absolute() or '..' in p.parts or path.startswith('.git'):
                raise ValueError('Allowed changes must be explicit relative paths inside the worktree')
        for check in spec.checks:
            check.oracle_revisions = {str(Path(arg).resolve()): hashlib.sha256(Path(arg).read_bytes()).hexdigest()
                for arg in check.argv[1:] if Path(arg).is_absolute() and Path(arg).is_file()}
        authorization = uuid.uuid4().hex
        self.store.execute('INSERT INTO implementation_authorizations VALUES(?,?,?,?,?,?,?)',
            (authorization, goal_id, current['goal']['revision'], current['policy']['version'],
             spec.model_dump_json(), 0, time.time()))
        self.control.event(goal_id, 'implementation_authorized', {'authorization': authorization})
        return authorization

    def auth(self, authorization):
        row = self.db.execute('SELECT * FROM implementation_authorizations WHERE id=?', (authorization,)).fetchone()
        if row is None or row['revoked']:
            raise ValueError('Authorization missing or revoked')
        current = self.control.current(row['goal_id'])
        if row['goal_revision'] != current['goal']['revision'] or row['policy_revision'] != current['policy']['version']:
            raise ValueError('Authorization is stale for the current requirements or policy')
        spec = ImplementationSpec.model_validate_json(row['spec'])
        for check in spec.checks:
            for path,digest in check.oracle_revisions.items():
                if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:
                    raise ValueError('Authorized acceptance oracle changed; reauthorization is required')
        job = self.db.execute('SELECT status FROM research_jobs WHERE id=?', (spec.research_job,)).fetchone()
        findings = self.db.execute('SELECT citations FROM knowledge WHERE job_id=? AND classification=?',
                                   (spec.research_job, 'verified')).fetchall()
        if job is None or job[0] != 'supported' or not findings:
            raise ValueError('Accepted research has no verified findings')
        if any(Knowledge(self.store).source(s)['stale'] for f in findings for s in json.loads(f[0])):
            raise ValueError('Accepted research evidence changed; research must be refreshed')
        return dict(row), spec

    def create(self, authorization):
        auth, _ = self.auth(authorization)
        goal_id = auth['goal_id']
        existing = self.db.execute('SELECT id FROM implementations WHERE authorization=?', (authorization,)).fetchone()
        if existing:
            return existing[0]
        repo = Path(self.store.snapshot(goal_id)['goal']['workspace'])
        base = git(repo, 'rev-parse', 'HEAD^{commit}')
        implementation_id = uuid.uuid4().hex
        worktree = self.store.path.parent / 'worktrees' / implementation_id
        self.store.execute('INSERT INTO implementations VALUES(?,?,?,?,?,?,?,?,?,?)',
            (implementation_id, goal_id, authorization, base, None, str(worktree), 'authorized', 0, '', time.time()))
        return implementation_id

    def get(self, implementation_id):
        row = self.db.execute('SELECT * FROM implementations WHERE id=?', (implementation_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown implementation')
        return dict(row)

    def prepare(self, row):
        repo = self.store.snapshot(row['goal_id'])['goal']['workspace']
        path = Path(row['worktree'])
        action_id = f"worktree:{row['id']}"
        self.store.execute('INSERT OR IGNORE INTO external_actions VALUES(?,?,?,?,?,?,?)',
            (action_id, row['goal_id'], 'git_worktree', str(path), 'pending', None, time.time()))
        if path.exists():
            # A missing response is not proof the previous git operation failed.
            if Path(git(path, 'rev-parse', '--git-common-dir')).resolve() != Path(git(repo, 'rev-parse', '--git-common-dir')).resolve():
                # Relative common dirs must be resolved against their checkout.
                left = (path / git(path, 'rev-parse', '--git-common-dir')).resolve()
                right = (Path(repo) / git(repo, 'rev-parse', '--git-common-dir')).resolve()
                if left != right:
                    raise ValueError('Existing worktree belongs to another repository')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git(repo, 'worktree', 'add', '--detach', str(path), row['base_revision'])
        self.store.execute("UPDATE external_actions SET state='confirmed',result=? WHERE id=?",
                           (git(path, 'rev-parse', 'HEAD'), action_id))
        return path

    async def run(self, implementation_id, task_id=None):
        row = self.get(implementation_id)
        auth, spec = self.auth(row['authorization'])
        if row['status'] in ('ready_for_audit', 'completed', 'needs_research', 'escalated', 'uncertain'):
            return row
        if row['attempts'] >= spec.max_attempts:
            self.store.execute("UPDATE implementations SET status='escalated' WHERE id=?", (implementation_id,))
            return self.get(implementation_id)
        workspace = self.prepare(row)
        try:
            while row['attempts'] < spec.max_attempts:
                auth, spec = self.auth(row['authorization'])
                attempt = row['attempts']+1
                self.store.execute("UPDATE implementations SET status='running',attempts=? WHERE id=?", (attempt, implementation_id))
                self.control.event(row['goal_id'], 'implementation_started', {'id': implementation_id, 'attempt': attempt})
                findings = [dict(r) for r in self.db.execute('SELECT title,body FROM knowledge WHERE job_id=?', (spec.research_job,))]
                prompt = json.dumps({'objective': spec.objective, 'research': findings,
                    'constraints': self.control.current(row['goal_id']), 'allowed_paths': spec.allowed_paths,
                    'checks': [c.model_dump() for c in spec.checks], 'feedback': row['feedback'],
                    'instructions': 'Implement only this objective in this worktree. Run tests with an OS-enforced timeout '
                        f'of at most {min(60, max(1, spec.seconds // 2))} seconds per command (for example timeout or subprocess.run(timeout=...)). '
                        'Do not keep polling an idle test indefinitely or repeat a timed-out command unchanged. If a test hangs or the sandbox prevents it, '
                        'terminate that command and report the exact limitation in the handoff; the independent verifier '
                        'will still run all authorized acceptance checks. Do not commit, deploy, access production, '
                        'or run destructive commands. Ask manager if a decision is needed. Return artifacts and unresolved concerns.'})
                thread_id, turn_id = await self.runtime.start(str(workspace), prompt, write=True,schema=ImplementationHandoff.model_json_schema(),
                    on_thread=(lambda sid: self.store.execute('UPDATE task_runtime SET session_id=? WHERE task_id=?',(sid,task_id))) if task_id else None)
                if task_id:
                    self.store.execute('UPDATE task_runtime SET session_id=?,turn_id=? WHERE task_id=?', (thread_id,turn_id,task_id))
                    self.store.execute('UPDATE tasks SET session_id=? WHERE id=?', (thread_id,task_id))
                self.control.message(row['goal_id'], 'implementation_session', {'thread_id': thread_id, 'turn_id': turn_id,
                    'implementation_id': implementation_id}, direction='worker_to_manager', state='acknowledged')
                turn = await self.runtime.wait(thread_id, turn_id, spec.seconds)
                handoff = {'summary':'Worker did not return a valid structured handoff', 'artifacts':[], 'tests_run':[],
                           'unresolved_concerns':['Worker concerns are unknown; rely on independent verification and inspect the native session'], 'questions':[]}
                for item in turn.get('items',[]):
                    if item.get('type')=='agentMessage':
                        try:
                            handoff=ImplementationHandoff.model_validate_json(item['text']).model_dump()
                        except (ValueError,KeyError):
                            pass
                self.control.message(row['goal_id'],'implementation_handoff',{'implementation_id':implementation_id,
                    'thread_id':thread_id,'turn_id':turn_id,'report':handoff},
                    direction='worker_to_manager',state='acknowledged')
                if turn['status'] != 'completed':
                    raise RuntimeError('Implementation turn did not complete; reconcile before another attempt')
                self.auth(row['authorization'])  # fence a concurrent policy or requirement update
                changed = git(workspace, 'status', '--porcelain', '-uall').splitlines()
                # Use NUL-delimited path listing to avoid quoting and rename ambiguities.
                tracked = subprocess.check_output(['git','-C',str(workspace),'diff',row['base_revision'],'--no-renames','--name-only','-z']).decode().split('\0')
                untracked = subprocess.check_output(['git','-C',str(workspace),'ls-files','--others','--exclude-standard','-z']).decode().split('\0')
                if any(p and not any(p == a or p.startswith(a.rstrip('/')+'/') for a in spec.allowed_paths) for p in tracked+untracked):
                    raise ValueError('Implementation changed paths outside explicit authorization')
                if changed:
                    git(workspace, 'add', '--all')
                    git(workspace, '-c', 'user.name=Omnitrix', '-c', 'user.email=omnitrix@localhost',
                        'commit', '-m', f'Candidate {implementation_id} attempt {attempt}')
                candidate = git(workspace, 'rev-parse', 'HEAD^{commit}')
                self.store.execute('UPDATE implementations SET candidate=? WHERE id=?', (candidate, implementation_id))
                results = await self.verify(implementation_id, candidate, auth, spec)
                failed = [r for r in results if not r['passed']]
                if not failed:
                    self.store.execute("UPDATE implementations SET status='ready_for_audit' WHERE id=?", (implementation_id,))
                    return self.get(implementation_id)
                feedback = json.dumps(failed)
                if any(r['failure_kind'] == 'assumption' for r in failed):
                    self.store.execute("UPDATE implementations SET status='needs_research',feedback=? WHERE id=?", (feedback, implementation_id))
                    self.control.event(row['goal_id'], 'research_required', {'implementation': implementation_id, 'feedback': failed})
                    return self.get(implementation_id)
                self.store.execute("UPDATE implementations SET status='repair',feedback=? WHERE id=?", (feedback, implementation_id))
                row = self.get(implementation_id)
            self.store.execute("UPDATE implementations SET status='escalated' WHERE id=?", (implementation_id,))
        except asyncio.CancelledError:
            self.store.execute("UPDATE implementations SET status='uncertain' WHERE id=?", (implementation_id,))
            raise
        except Exception as exc:
            self.store.execute("UPDATE implementations SET status='uncertain',feedback=? WHERE id=?", (str(exc), implementation_id))
        return self.get(implementation_id)

    async def reconcile(self, implementation_id, thread_result=None):
        row=self.get(implementation_id)
        auth,spec=self.auth(row['authorization'])
        references=[json.loads(m[0]) for m in self.db.execute("SELECT body FROM messages WHERE goal_id=? AND kind='implementation_session' ORDER BY created DESC",(row['goal_id'],))]
        reference=next((r for r in references if r.get('implementation_id')==implementation_id),None)
        if reference is None:
            active=self.db.execute("SELECT session_id,turn_id FROM task_runtime WHERE json_extract(spec,'$.implementation_id')=? ORDER BY started DESC LIMIT 1",(implementation_id,)).fetchone()
            if active and active['session_id'] and active['turn_id']:
                reference={'thread_id':active['session_id'],'turn_id':active['turn_id']}
        if thread_result is None:
            if reference is None:
                return {'blocked':'No confirmed external session; inspect worktree and task runtime before authorizing replacement'}
            thread_result=await self.runtime.reconcile(reference['thread_id'])
        turns=thread_result['thread'].get('turns',[])
        turn=next((t for t in turns if reference and t['id']==reference['turn_id']),None)
        if turn is None:
            return {'blocked':'Exact external turn outcome is not available; no coding action was replayed'}
        if turn['status'] in ('failed','interrupted'):
            status='repair' if row['attempts']<spec.max_attempts else 'escalated'
            self.store.execute('UPDATE implementations SET status=?,feedback=? WHERE id=?',
                (status,'External turn termination reconciled; inspect retained worktree before a bounded repair',implementation_id))
            return self.get(implementation_id)
        if turn['status']!='completed':
            return {'blocked':'External turn is still active; interrupt or await it before retry'}
        workspace=Path(row['worktree'])
        changed=subprocess.check_output(['git','-C',str(workspace),'diff',row['base_revision'],'--no-renames','--name-only','-z']).decode().split('\0')
        changed+=subprocess.check_output(['git','-C',str(workspace),'ls-files','--others','--exclude-standard','-z']).decode().split('\0')
        if any(p and not any(p==a or p.startswith(a.rstrip('/')+'/') for a in spec.allowed_paths) for p in changed):
            raise ValueError('Reconciled candidate changed unauthorized paths')
        if git(workspace,'status','--porcelain'):
            git(workspace,'add','--all')
            git(workspace,'-c','user.name=Omnitrix','-c','user.email=omnitrix@localhost','commit','-m',f'Reconciled candidate {implementation_id}')
        candidate=git(workspace,'rev-parse','HEAD^{commit}')
        self.store.execute('UPDATE implementations SET candidate=? WHERE id=?',(candidate,implementation_id))
        results=await self.verify(implementation_id,candidate,auth,spec)
        failed=[r for r in results if not r['passed']]
        status='ready_for_audit' if not failed else 'needs_research' if any(r['failure_kind']=='assumption' for r in failed) else 'repair' if row['attempts']<spec.max_attempts else 'escalated'
        self.store.execute('UPDATE implementations SET status=?,feedback=? WHERE id=?',(status,json.dumps(failed),implementation_id))
        self.control.event(row['goal_id'],'implementation_reconciled',{'implementation':implementation_id,'candidate':candidate,'status':status})
        return self.get(implementation_id)

    async def verify(self, implementation_id, candidate, auth, spec):
        row = self.get(implementation_id)
        repo = self.store.snapshot(row['goal_id'])['goal']['workspace']
        verification = self.store.path.parent / 'verification' / f'{implementation_id}-{row["attempts"]}'
        verification.parent.mkdir(parents=True, exist_ok=True)
        if not verification.exists():
            git(repo, 'worktree', 'add', '--detach', str(verification), candidate)
        if git(verification, 'rev-parse', 'HEAD') != candidate or git(verification, 'status', '--porcelain'):
            raise ValueError('Verification worktree must be clean at the exact candidate revision')
        results = []
        for check in spec.checks:
            artifact = self.store.path.parent / 'artifacts' / f'{uuid.uuid4().hex}.log'
            artifact.parent.mkdir(parents=True, exist_ok=True)
            # No manager/provider credentials are passed to test processes.
            env = {'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'HOME': str(verification),
                   'OMNITRIX_CANDIDATE': str(verification), 'PYTHONDONTWRITEBYTECODE': '1'}
            with artifact.open('wb') as log:
                proc = await asyncio.create_subprocess_exec(*check.argv, cwd=verification, env=env,
                    stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                try:
                    await asyncio.wait_for(proc.wait(), check.timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                    if asyncio.current_task().cancelling():
                        raise
            output = artifact.read_bytes()
            passed = proc.returncode == 0 and (check.stdout_contains is None or check.stdout_contains in output.decode(errors='replace'))
            result = {'requirement': check.requirement, 'passed': passed, 'exit_code': proc.returncode,
                      'artifact': str(artifact), 'failure_kind': check.failure_kind}
            self.store.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, implementation_id, candidate, auth['goal_revision'], check.requirement,
                 json.dumps({'argv':check.argv,'oracle_revisions':check.oracle_revisions}), int(passed), proc.returncode, str(artifact), hashlib.sha256(output).hexdigest(),
                 check.failure_kind, time.time()))
            results.append(result)
        # Test commands may not mutate candidate source or HEAD and still claim verification.
        if git(verification, 'rev-parse', 'HEAD') != candidate or git(verification, 'diff', '--name-only'):
            raise ValueError('Verifier modified the candidate; evidence is invalid')
        return results

    def audit(self, implementation_id):
        row = self.get(implementation_id)
        auth, spec = self.auth(row['authorization'])
        current = self.control.current(row['goal_id'])
        results = [dict(r) for r in self.db.execute('SELECT * FROM verifications WHERE implementation_id=? AND candidate=? AND goal_revision=?',
                                                  (implementation_id, row['candidate'], auth['goal_revision']))]
        required = current['goal']['requirements']
        passed = row['status'] == 'ready_for_audit' and all(any(r['requirement'] == c and r['passed'] for r in results) for c in required)
        # Verify immutable test artifacts still match their saved content digest.
        passed = passed and all(Path(r['artifact']).exists() and hashlib.sha256(Path(r['artifact']).read_bytes()).hexdigest() == r['digest'] for r in results)
        with self.db:
            self.db.execute('INSERT INTO completion_audits VALUES(?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, row['goal_id'], current['goal']['revision'], row['candidate'] or '',
                 json.dumps(required), int(passed), time.time()))
            if passed:
                self.db.execute("UPDATE implementations SET status='completed' WHERE id=?", (implementation_id,))
                self.db.execute("UPDATE goals SET status='completed' WHERE id=?", (row['goal_id'],))
        self.control.event(row['goal_id'], 'goal_audit', {'implementation': implementation_id, 'passed': passed, 'candidate': row['candidate']})
        return {'passed': passed, 'candidate': row['candidate'], 'requirements': required}
