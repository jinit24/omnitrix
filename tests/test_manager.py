import asyncio
import json
import pytest
from omnitrix.manager import Manager, AgentBrain
from omnitrix.models import Decision, Handoff
from omnitrix.store import Store


def decision(action='delegate', task_id=None):
    return Decision(action=action, rationale='Evaluate repository evidence',
                    instructions='Inspect README and report the setup requirements',
                    task_id=task_id, priority=1)


class Brain:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.seen = []

    async def decide(self, state):
        self.seen.append(state)
        value = next(self.actions)
        return value(state) if callable(value) else value


class Worker:
    name = 'codex'

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def run(self, instructions, workspace, session_id, on_session):
        self.calls.append((instructions, workspace, session_id))
        on_session(session_id or 'session-123')
        if self.fail:
            raise RuntimeError('test transport failure')
        return Handoff(status='completed', summary='Found setup instructions',
                       evidence=['README.md:1'], next_steps=[])


def setup(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Understand project setup', ['Cite setup instructions'], tmp_path)
    return store, goal


async def test_delegation_restart_followup_and_completion(tmp_path):
    store, goal = setup(tmp_path)
    worker = Worker()
    first = await Manager(store, Brain(decision()), worker).resume(goal, steps=1)
    task_id = first['tasks'][0]['id']
    assert first['tasks'][0]['status'] == 'completed'
    store.db.close()
    store = Store(tmp_path / 'state.db')
    brain = Brain(decision('follow_up', task_id), decision('complete'))
    final = await Manager(store, brain, worker).resume(goal, message='Verify the evidence')
    assert worker.calls[1][2] == 'session-123'
    assert final['goal']['status'] == 'completed'
    assert len(final['decisions']) == 4
    assert any(d.get('user_message') for d in brain.seen[0]['decisions'])
    assert json.loads(brain.seen[0]['tasks'][0]['result'])['evidence'] == ['README.md:1']
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2


async def test_failure_is_returned_to_manager(tmp_path):
    store, goal = setup(tmp_path)
    brain = Brain(decision(), decision('pause'))
    final = await Manager(store, brain, Worker(fail=True)).resume(goal)
    assert final['goal']['status'] == 'paused'
    assert brain.seen[1]['tasks'][0]['error'] == 'test transport failure'
    assert final['tasks'][0]['session_id'] == 'session-123'


async def test_interrupted_attempt_is_not_blindly_replayed(tmp_path):
    store, goal = setup(tmp_path)
    await Manager(store, Brain(decision()), Worker()).resume(goal, steps=1)
    store.execute("UPDATE tasks SET status='running'")
    store.execute("UPDATE attempts SET status='running'")
    brain = Brain(decision('pause'))
    worker = Worker()
    await Manager(store, brain, worker).resume(goal)
    assert brain.seen[0]['tasks'][0]['status'] == 'interrupted'
    assert not worker.calls
    assert store.db.execute('SELECT status FROM attempts').fetchone()[0] == 'interrupted'


async def test_cross_goal_followup_rejected(tmp_path):
    store, goal = setup(tmp_path)
    first = await Manager(store, Brain(decision()), Worker()).resume(goal, steps=1)
    other = store.create_goal('Other', ['Other'], tmp_path)
    with pytest.raises(ValueError, match='belong'):
        await Manager(store, Brain(decision('follow_up', first['tasks'][0]['id'])), Worker()).resume(other)


async def test_completed_goal_does_not_repeat_work(tmp_path):
    store, goal = setup(tmp_path)
    await Manager(store, Brain(decision('complete')), Worker()).resume(goal)
    assert (await Manager(store, Brain(), Worker()).resume(goal))['goal']['status'] == 'completed'


async def test_sdk_agent_uses_tools_and_structured_decision(tmp_path, monkeypatch):
    store, goal = setup(tmp_path)
    async def fake_run(agent, prompt, **kwargs):
        assert agent.output_type is Decision
        assert agent.tools[0].name == 'recall_history'
        assert json.loads(prompt)['goal']['objective'] == 'Understand project setup'
        assert kwargs['max_turns'] == 8
        class Result:
            final_output = decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run', fake_run)
    result = await AgentBrain().decide(store.snapshot(goal))
    assert result.action == 'pause'


async def test_database_lock_prevents_overlapping_managers(tmp_path):
    store, goal = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    class WaitingBrain:
        async def decide(self, state):
            entered.set()
            await release.wait()
            return decision('pause')
    active = asyncio.create_task(Manager(store, WaitingBrain(), Worker()).resume(goal))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match='owns'):
            await Manager(Store(store.path), Brain(decision()), Worker()).resume(goal)
    finally:
        release.set()
        await active


async def test_manager_sqlite_tools_use_real_sdk_dispatch(tmp_path,monkeypatch):
    from agents.tool_context import ToolContext
    store,goal=setup(tmp_path)
    async def fake_run(agent,prompt,**kwargs):
        for tool,args in [(agent.tools[0],{'section':'messages','offset':0}),
                          (agent.tools[1],{'query':'','offset':0}),
                          (agent.tools[2],{'directory':'.'})]:
            payload=json.dumps(args)
            ctx=ToolContext(context=None,tool_name=tool.name,tool_call_id=tool.name,tool_arguments=payload)
            output=await tool.on_invoke_tool(ctx,payload)
            parsed = json.loads(output)
            if tool.name == 'list_resources':
                assert isinstance(parsed['entries'], list)
                assert 'next_offset' in parsed
            else:
                assert isinstance(parsed, list)
        class Result:
            final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action=='pause'


async def test_inventory_cannot_consume_the_entire_decision_turn_budget(tmp_path, monkeypatch):
    from agents.tool_context import ToolContext
    store, goal = setup(tmp_path)
    for index in range(180):
        (tmp_path / f'source-{index:03}.py').write_text('pass\n')
    async def fake_run(agent, prompt, **kwargs):
        assert json.loads(prompt)['manager_tool_budget'] == 4
        inventory = next(tool for tool in agent.tools if tool.name == 'list_resources')
        for index in range(4):
            assert inventory.is_enabled(None, agent)
            payload = json.dumps({'directory': '.', 'offset': index * 40})
            ctx = ToolContext(context=None, tool_name=inventory.name, tool_call_id=str(index), tool_arguments=payload)
            page = json.loads(await inventory.on_invoke_tool(ctx, payload))
            assert page['remaining_manager_tool_calls'] == 3 - index
        assert page['next_offset'] is not None  # More files do not justify an infinite inventory loop.
        assert all(not tool.is_enabled(None, agent) for tool in agent.tools)
        class Result:
            final_output = decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run', fake_run)
    await AgentBrain(store=store).decide(store.snapshot(goal))


async def test_usage_is_retained_when_manager_exhausts_turns(tmp_path, monkeypatch):
    from agents.exceptions import MaxTurnsExceeded
    from agents.items import ModelResponse
    from agents.usage import Usage
    store, goal = setup(tmp_path)
    async def fake_run(agent, prompt, **kwargs):
        for response_id in ('one', 'two'):
            response = ModelResponse(output=[], usage=Usage(requests=1, input_tokens=100, output_tokens=20),
                                     response_id=response_id)
            await kwargs['hooks'].on_llm_end(None, agent, response)
        raise MaxTurnsExceeded('Turn cap after successful provider responses')
    monkeypatch.setattr('omnitrix.manager.Runner.run', fake_run)
    with pytest.raises(MaxTurnsExceeded):
        await AgentBrain(store=store).decide(store.snapshot(goal))
    usage = [json.loads(row[0]) for row in store.db.execute("SELECT detail FROM activity WHERE kind='usage'")]
    assert sum(row['input_tokens'] for row in usage) == 200
    assert sum(row['output_tokens'] for row in usage) == 40


async def test_ordinary_decisions_use_bounded_state_and_preserve_handoffs_after_restart(tmp_path, monkeypatch):
    from omnitrix.control import Control
    store, goal = setup(tmp_path)
    control = Control(store)
    control.message(goal, 'chat', {'text': 'Preserve the public API'}, state='acknowledged')
    control.message(goal, 'implementation_handoff', {'implementation_id': 'candidate-id',
        'report': {'summary': 'Repair prepared', 'unresolved_concerns': ['Async check timed out'],
                   'tests_run': ['Focused check passed'], 'questions': []}}, direction='worker_to_manager')
    for index in range(1000):
        store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',
            (goal, json.dumps({'index': index, 'rationale': 'x' * 1000})))
    store.db.close()
    store = Store(tmp_path / 'state.db')
    async def fake_run(agent, prompt, **kwargs):
        state = json.loads(prompt)
        assert kwargs['session'] is None
        assert len(state['decisions']) == 8
        assert len(prompt) < 20000
        assert 'Preserve the public API' in prompt and 'Async check timed out' in prompt
        return type('Result', (), {'final_output': decision('pause')})()
    monkeypatch.setattr('omnitrix.manager.Runner.run', fake_run)
    await AgentBrain(store=store).decide(store.snapshot(goal))
    assert store.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 1000
