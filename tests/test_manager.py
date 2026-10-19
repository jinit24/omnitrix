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
            decoded=json.loads(output)
            if tool.name == 'list_resources':
                assert isinstance(decoded['entries'],list)
                assert 'next_offset' in decoded
            else:
                assert isinstance(decoded,list)
        class Result:
            final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action=='pause'


async def test_manager_context_avoids_duplicate_reports_and_retains_recall(tmp_path,monkeypatch):
    store,goal=setup(tmp_path)
    state=store.snapshot(goal)
    state['tasks']=[{'id':'task','status':'completed','result':'duplicated large report','instructions':'large old assignment'}]
    state['worker_messages']=[{'kind':'findings','body':'structured report '+str(i)} for i in range(12)]
    async def fake_run(agent,prompt,**kwargs):
        context=json.loads(prompt)
        assert 'result' not in context['tasks'][0]
        assert len(context['worker_messages'])==3
        assert 'structured report 0' in prompt
        class Result: final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(state)).action=='pause'


async def test_manager_reads_catalog_without_research_and_restricts_code_reads(tmp_path,monkeypatch):
    from agents.tool_context import ToolContext
    store,goal=setup(tmp_path)
    (tmp_path/'catalog').mkdir()
    (tmp_path/'catalog'/'part.jsonl').write_text('{"locator":"sources/controller.go"}\n')
    (tmp_path/'source.py').write_text('private source read requires research')
    async def fake_run(agent,prompt,**kwargs):
        tool=next(t for t in agent.tools if t.name=='read_catalog')
        async def invoke(locator):
            args=json.dumps({'locator':locator,'start_line':1,'line_count':80})
            ctx=ToolContext(context=None,tool_name=tool.name,tool_call_id='catalog-read',tool_arguments=args)
            return await tool.on_invoke_tool(ctx,args)
        assert 'sources/controller.go' in await invoke('catalog/part.jsonl')
        assert 'typed research' in await invoke('source.py')
        assert store.db.execute("SELECT count(*) FROM activity WHERE kind='catalog_read'").fetchone()[0]==1
        class Result: final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action=='pause'


def test_history_filter_retains_conversation_without_repeated_state_snapshots():
    from omnitrix.manager import merge_manager_history
    state={'role':'user','content':json.dumps({'goal':{'id':'goal'},'tasks':[],'decisions':[]})}
    chat={'role':'user','content':'Prioritize correctness over speed.'}
    response={'role':'assistant','content':'Investigate the contract.'}
    current={'role':'user','content':'current snapshot'}
    history=[state,chat,response]
    assert merge_manager_history(history,[current])==[chat,response,current]
    assert history==[state,chat,response]


def test_current_context_does_not_duplicate_pending_findings(tmp_path):
    from omnitrix.manager import manager_context
    state={'tasks':[],'decisions':[],'pending_messages':[{'id':'finding','kind':'findings','body':'large report','state':'pending'}], 'worker_messages':[{'kind':'findings','body':'large report'}]}
    compact=manager_context(state)
    assert 'body' not in compact['pending_messages'][0]
    assert compact['worker_messages'][0]['body']=='large report'
    assert state['pending_messages'][0]['body']=='large report'


async def test_report_tool_pages_complete_unicode_report_and_fences_other_goal(tmp_path,monkeypatch):
    from agents.tool_context import ToolContext
    store,goal=setup(tmp_path)
    other=store.create_goal('Other goal',[],tmp_path)
    report={'findings':[{'statement':'Quote " and snowman \u2603 '*300}], 'uncertainties':['Check the counterexample']}
    store.execute("INSERT INTO research_jobs VALUES('job',?,'research','signature','{}','supported',1,1,1)",(goal,))
    store.execute("INSERT INTO research_runs(id,job_id,status,report,evaluation,created) VALUES('run','job','supported',?,'{}',1)",(json.dumps(report,ensure_ascii=False),))
    store.execute("INSERT INTO research_jobs VALUES('other',?,'research','other-signature','{}','supported',1,1,1)",(other,))
    store.execute("INSERT INTO research_runs(id,job_id,status,report,created) VALUES('other-run','other','supported','private',1)")
    async def fake_run(agent,prompt,**kwargs):
        tool=next(t for t in agent.tools if t.name=='read_report')
        async def invoke(job_id,offset):
            args=json.dumps({'job_id':job_id,'section':'report','offset':offset})
            context=ToolContext(context=None,tool_name=tool.name,tool_call_id=f'read-{offset}',tool_arguments=args)
            return await tool.on_invoke_tool(context,args)
        offset=0;chunks=[]
        while True:
            raw=await invoke('job',offset)
            assert len(raw.encode())<=4096
            page=json.loads(raw)
            chunks.append(page['text'])
            if page['next_offset'] is None:break
            assert page['next_offset']>offset
            offset=page['next_offset']
        assert json.loads(json.loads(''.join(chunks))['content']) == report
        assert 'this goal' in await invoke('other',0)
        class Result: final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action=='pause'


async def test_blocker_card_from_real_snapshot_retains_retrievable_durable_message(tmp_path,monkeypatch):
    from agents.tool_context import ToolContext
    from omnitrix.control import Control
    store,goal=setup(tmp_path)
    body={'reason':'Reachability is still unknown. '*100,'unresolved':'Preserve this late detail'}
    message=Control(store).message(goal,'blocker',body,direction='worker_to_manager')
    async def fake_run(agent,prompt,**kwargs):
        card=json.loads(prompt)['worker_messages'][0]
        assert card['detail_ref']=={'section':'messages','id':message,'task_id':None}
        assert card['direction']=='worker_to_manager'
        tool=next(t for t in agent.tools if t.name=='retrieve_record')
        offset=0;chunks=[]
        while True:
            args=json.dumps({'section':'messages','record_id':card['detail_ref']['id'],'offset':offset})
            context=ToolContext(context=None,tool_name=tool.name,tool_call_id=f'message-{offset}',tool_arguments=args)
            page=json.loads(await tool.on_invoke_tool(context,args))
            chunks.append(page['text'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        assert json.loads(json.loads(''.join(chunks))['body'])==body
        class Result: final_output=decision('pause')
        return Result()
    monkeypatch.setattr('omnitrix.manager.Runner.run',fake_run)
    assert (await AgentBrain(store=store).decide(store.snapshot(goal))).action=='pause'


async def test_legacy_manager_executes_queued_approved_work_without_brain_call(tmp_path,monkeypatch):
    from omnitrix.campaign import Campaign
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan
    store,goal=setup(tmp_path)
    (tmp_path/'source.txt').write_text('A bounded source contract.\n')
    service=Research(store)
    job=service.plan(goal,'research',ResearchPlan(scope=['source.txt'],question='Is this bounded contract present?',
        hypothesis='The source has a bounded contract',falsification='The source omits the contract'))['job_id']
    Campaign(store,goal).enqueue(job,2,'Read the approved scope')
    class NoBrain:
        async def decide(self,state):pytest.fail('Ready approved work should not call the model')
    async def attempt(self,job_id,feedback=''):
        assert job_id==job and feedback=='Read the approved scope'
        return {'job_id':job_id,'status':'supported'}
    monkeypatch.setattr('omnitrix.manager.Research.attempt',attempt)
    await Manager(store,NoBrain(),None).resume(goal,steps=1)
    assert Campaign(store,goal).summary()['queue']['completed']==1


def test_history_pages_never_replay_orphan_tool_outputs_or_calls():
    from omnitrix.manager import merge_manager_history
    history=[{'type':'function_call_output','call_id':'missing-call','output':'old navigation'},
             {'type':'function_call','call_id':'paired','name':'list_resources','arguments':'{}'},
             {'type':'function_call_output','call_id':'paired','output':'valid navigation'},
             {'type':'function_call','call_id':'missing-output','name':'list_resources','arguments':'{}'}]
    current={'role':'user','content':'fresh durable state'}
    assert merge_manager_history(history,[current])==[history[1],history[2],current]
    assert len(history)==4
