"""Prompt delivery at real agent/adapter boundaries; no paid model calls."""
import json
import shutil
import sys
from types import SimpleNamespace

import pytest

from omnitrix.manager import AgentBrain
from omnitrix.models import ChatReply, Decision, Handoff
from omnitrix.prompts import ROOT, load_instructions
from omnitrix.research import Research, ResearchAgents
from omnitrix.research_models import DiscoveryReport, ResearchPlan
from omnitrix.knowledge import ResearchTools
from omnitrix.runtime import CodexRuntime
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor
from omnitrix.workers import CodexWorker
from tests_helpers import FixtureAgents


@pytest.mark.parametrize('role', ['manager', 'chat', 'discovery', 'research', 'evaluation'])
async def test_sdk_entry_points_receive_shared_and_distinct_role_instructions(tmp_path, monkeypatch, role):
    (tmp_path / 'system.md').write_text('Polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Inspect polling', ['Cite the source'], tmp_path)
    captured = []

    async def fake_run(agent, prompt, **kwargs):
        captured.append(agent)
        if role == 'manager':
            result = Decision(action='pause', rationale='Need a bounded source check',
                              instructions='Inspect polling', task_id=None, priority=1)
        elif role == 'chat':
            result = ChatReply(response='No accepted finding yet', proposal_kind='none',
                               proposal_text='', affected_tasks=[], add_requirements=[])
        else:
            result = SimpleNamespace()  # This test inspects delivery, not model reasoning.
        return SimpleNamespace(final_output=result)

    monkeypatch.setattr('omnitrix.manager.Runner.run', fake_run)
    if role == 'manager':
        await AgentBrain(store=store).decide(store.snapshot(goal))
    elif role == 'chat':
        await Supervisor(store, goal).chat('What is verified?')
    else:
        service = Research(store, FixtureAgents())
        job = service.plan(goal, 'discovery', ResearchPlan(
            scope=['system.md'], question='Which polling assumption needs evidence?'))['job_id']
        await service.attempt(job)
        run = store.db.execute('SELECT id FROM research_runs').fetchone()[0]
        await ResearchAgents().run(role, {}, ResearchTools(store, run, str(tmp_path), ['system.md'], 2))

    assert len(captured) == 1
    instructions = captured[0].instructions
    assert (ROOT / 'instructions/QUALITY_GATES.md').read_text() in instructions
    assert (ROOT / f'instructions/{role.upper()}.md').read_text() in instructions
    assert ('# AgentBrain:' in instructions) == (role in ('manager', 'chat'))
    if role in ('discovery', 'research', 'evaluation'):
        assert 'quality_report' in captured[0].output_type.model_fields


@pytest.mark.parametrize('write', [False, True])
@pytest.mark.parametrize('session_id', [None, 'existing-thread'])
async def test_app_server_start_and_resume_use_role_policy_separate_from_task(monkeypatch, write, session_id):
    runtime = CodexRuntime()
    calls = []

    async def connect():
        pass

    async def call(method, params):
        calls.append((method, params))
        return {'turn': {'id': 'turn'}} if method == 'turn/start' else {'thread': {'id': 'thread'}}

    monkeypatch.setattr(runtime, 'connect', connect)
    monkeypatch.setattr(runtime, 'call', call)
    await runtime.start('.', 'Bounded task input', session_id=session_id, write=write)
    method, params = calls[0]
    assert method == ('thread/resume' if session_id else 'thread/start')
    assert params['developerInstructions'] == load_instructions('implementation' if write else 'worker')
    assert params['sandbox'] == ('workspace-write' if write else 'read-only')
    assert params['approvalPolicy'] == 'never'
    assert params['config']['sandbox_workspace_write.network_access'] is False
    assert calls[1][1]['input'] == [{'type': 'text', 'text': 'Bounded task input'}]


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for the SDK bridge')
async def test_real_node_bridge_delivers_worker_policy_and_preserves_report_on_resume(tmp_path, monkeypatch):
    # Exercise Python -> Node SDK -> CLI arguments with a fake local CLI, not a model.
    captured = tmp_path / 'invocation.json'
    executable = tmp_path / 'codex-fixture'
    report = '| Gate | Result |\n| --- | --- |\n| Scope | PASS: pyproject.toml |'
    handoff = dict(status='completed', summary='Inspected metadata', evidence=['pyproject.toml'],
                   next_steps=[], quality_report=report)
    executable.write_text(f'''#!{sys.executable}
import json, pathlib, sys
args = sys.argv[1:]
schema = json.loads(pathlib.Path(args[args.index('--output-schema') + 1]).read_text())
pathlib.Path({str(captured)!r}).write_text(json.dumps({{'args': args, 'input': sys.stdin.read(), 'schema': schema}}))
print(json.dumps({{'type': 'thread.started', 'thread_id': 'thread-1'}}))
print(json.dumps({{'type': 'item.completed', 'item': {{'id': 'message-1', 'type': 'agent_message', 'text': {json.dumps(handoff)!r}}}}}))
print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}}}}))
''')
    executable.chmod(0o755)
    monkeypatch.setenv('OMNITRIX_CODEX_PATH', str(executable))
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    worker = CodexWorker()
    for session in (None, 'thread-1'):
        result = await worker.run('Inspect metadata', str(tmp_path), session, lambda _: None)
        invocation = json.loads(captured.read_text())
        assert result.quality_report == report
        config = next(arg for arg in invocation['args'] if arg.startswith('developer_instructions='))
        assert json.loads(config.split('=', 1)[1]) == load_instructions('worker')
        assert invocation['input'] == 'Inspect metadata'
        assert 'quality_report' in invocation['schema']['required']
        assert ('resume' in invocation['args']) == bool(session)


async def test_research_quality_evidence_survives_evaluation_storage_and_export(tmp_path):
    (tmp_path / 'system.md').write_text('Polling interval=30 seconds.\n')
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Inspect polling', ['Cite polling evidence'], tmp_path)
    report = '```mermaid\nflowchart LR\n Queue --> Database\n```\nDiagram is illustrative; latency is unmeasured.'

    class WithReports(FixtureAgents):
        async def run(self, role, context, tools):
            if role == 'evaluation':
                assert context['report']['quality_report'] == report
            result = await super().run(role, context, tools)
            return result.model_copy(update={'quality_report': report})

    service = Research(store, WithReports())
    job = service.plan(goal, 'research', ResearchPlan(scope=['system.md'],
        question='What polling interval is configured?', hypothesis='Polling interval is 30 seconds',
        falsification='Source specifies a different interval'))['job_id']
    outcome = await service.attempt(job)
    assert outcome['report']['quality_report'] == report
    saved = store.db.execute('SELECT report,evaluation FROM research_runs').fetchone()
    assert all(json.loads(value)['quality_report'] == report for value in saved)
    exported = service.knowledge.export(goal)
    assert json.loads(exported['jobs'][0]['runs'][0]['report'])['quality_report'] == report


def test_legacy_handoffs_are_readable_without_fabricating_quality_evidence():
    old = Handoff.model_validate(dict(status='completed', summary='Old result', evidence=[], next_steps=[]))
    assert old.quality_report == ''
    assert DiscoveryReport.model_validate(dict(system_map=[], opportunities=[], gaps=[])).quality_report == ''


def test_missing_policy_cannot_silently_launch_an_unprotected_role(tmp_path, monkeypatch):
    monkeypatch.setattr('omnitrix.prompts.ROOT', tmp_path)
    with pytest.raises(FileNotFoundError):
        load_instructions('worker')
    with pytest.raises(KeyError):
        load_instructions('unregistered-role')
