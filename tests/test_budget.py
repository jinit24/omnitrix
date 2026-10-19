"""No paid calls: exercise real SDK turns through a fake HTTP transport."""
import asyncio
import json

import pytest
from agents import Agent, Runner, function_tool

from omnitrix.budget import (Budget, BudgetError, BudgetHTTPClient, BudgetModelProvider,
    assert_no_unmetered_worker, budget_enabled, budget_run_config, httpx)
from omnitrix.store import Store


PRICES = {'test-model': {'input_per_million': 5.5, 'output_per_million': 16.5}}


def configured(tmp_path, limit=25):
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Research the test repository', [], tmp_path)
    return store, goal, Budget.configure(store,goal,limit,PRICES)


def request_body(**changes):
    body = {'model':'test-model','input':'A bounded text prompt',
            'max_output_tokens':100,'service_tier':'default'}
    body.update(changes)
    return json.dumps(body).encode()


def test_reservation_is_durable_and_configuration_cannot_refill(tmp_path):
    store,goal,budget = configured(tmp_path)
    request = budget.reserve(request_body(), 'research')
    first = budget.summary()
    assert first['committed_usd'] > 0
    assert first['reserved_usd'] == first['committed_usd']
    assert first['unresolved_requests'] == 1
    store.db.close()
    reopened = Store(tmp_path/'state.db')
    budget = Budget.configure(reopened,goal,25,PRICES)
    assert budget.summary() == first
    with pytest.raises(BudgetError,match='immutable'):
        Budget.configure(reopened,goal,24,PRICES)
    budget.settle(request,{'id':'resp_known','usage':{'input_tokens':20,'output_tokens':10}})
    summary = budget.summary()
    assert summary['committed_usd'] == pytest.approx((20*5.5+10*16.5)/1_000_000)
    assert summary['reserved_usd'] == 0
    row = reopened.db.execute('SELECT * FROM dollar_requests').fetchone()
    assert row['response_id'] == 'resp_known'
    assert json.loads(row['usage'])['input_tokens'] == 20
    assert row['request_bytes'] == len(request_body())


def test_atomic_cap_preserves_last_reservation_across_connections(tmp_path):
    store,goal,budget = configured(tmp_path,0.04)
    budget.reserve(request_body(), 'research')
    other = Budget(Store(store.path),goal)
    with pytest.raises(BudgetError,match='exhausted'):
        other.reserve(request_body(), 'evaluation')
    assert budget.summary()['requests'] == 1
    assert budget.summary()['committed_usd'] <= 0.04


@pytest.mark.parametrize('change',[
    {'model':'unknown'}, {'service_tier':'priority'}, {'service_tier':None},
    {'reasoning':{'mode':'pro','effort':'low'}},
    {'max_output_tokens':None}, {'max_output_tokens':False}, {'stream':True},
    {'previous_response_id':'resp_server_history'}, {'conversation':'conv_server_history'},
    {'prompt':{'id':'pmpt_unknown'}}, {'background':True},
    {'tools':[{'type':'web_search'}]},
    {'input':[{'type':'item_reference','id':'msg_server_history'}]},
    {'input':[{'type':'reasoning','id':'rs_server_history','summary':[]}]},
    {'input':[{'type':'reasoning','id':'rs_encrypted','encrypted_content':'opaque','summary':[]}]},
    {'input':[{'type':'future_unknown','id':'unknown'}]},
    {'input':[{'type':'message','content':[{'type':'input_image','image_url':'https://example.test/image'}]}]},
])
def test_unbounded_or_additionally_billed_features_fail_before_reserving(tmp_path,change):
    store,goal,budget = configured(tmp_path)
    with pytest.raises(BudgetError):
        budget.reserve(request_body(**change),'research')
    assert budget.summary()['requests'] == 0


def test_input_bound_includes_actual_schema_history_and_utf8_bytes(tmp_path):
    store,goal,budget = configured(tmp_path)
    small = budget.reserve(request_body(),'research')
    long_body = request_body(input=[{'type':'message','role':'user','content':'\u263a'*1000}],
                             tools=[{'type':'function','name':'read','parameters':{'type':'object','description':'x'*4000}}])
    large = budget.reserve(long_body,'research')
    rows = {row['id']:dict(row) for row in store.db.execute('SELECT * FROM dollar_requests')}
    assert rows[large]['input_bound'] >= len(long_body)
    assert rows[large]['input_bound'] > rows[small]['input_bound'] + 5000


async def test_http_error_and_cancellation_are_charged_conservatively_without_retry(tmp_path):
    store,goal,budget = configured(tmp_path)
    calls = []
    async def fail(request):
        calls.append(request)
        return httpx.Response(429,json={'error':{'message':'limited'}})
    async with BudgetHTTPClient(budget,'research',transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(BudgetError,match='429'):
            await client.post('https://example.test/v1/responses',content=request_body())
    entered = asyncio.Event()
    async def hang(request):
        entered.set()
        await asyncio.Future()
    async with BudgetHTTPClient(budget,'evaluation',transport=httpx.MockTransport(hang)) as client:
        task = asyncio.create_task(client.post('https://example.test/v1/responses',content=request_body()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1
    assert budget.summary()['unresolved_requests'] == 2
    assert budget.summary()['reserved_usd'] == budget.summary()['committed_usd']
    assert {row[0] for row in store.db.execute('SELECT state FROM dollar_requests')} == {'uncertain'}


async def test_insufficient_funds_never_reaches_http_transport(tmp_path):
    store,goal,budget = configured(tmp_path,0.001)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={})
    async with BudgetHTTPClient(budget,'research',transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BudgetError,match='exhausted'):
            await client.post('https://example.test/v1/responses',content=request_body())
    assert calls == []


def test_missing_usage_keeps_full_reservation_and_overrun_stops_future_requests(tmp_path):
    store,goal,budget = configured(tmp_path)
    missing = budget.reserve(request_body(),'research')
    with pytest.raises(BudgetError,match='usage missing'):
        budget.settle(missing,{'id':'resp_no_usage'})
    assert budget.summary()['reserved_usd'] > 0
    overrun = budget.reserve(request_body(),'evaluation')
    with pytest.raises(BudgetError,match='exceeded'):
        budget.settle(overrun,{'id':'resp_overrun','usage':{'input_tokens':10,'output_tokens':101}})
    with pytest.raises(BudgetError,match='inspection'):
        budget.reserve(request_body(),'manager')


def test_unconfigured_goals_preserve_behavior_and_codex_is_blocked_when_configured(tmp_path):
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Legacy behavior',[],tmp_path)
    assert not budget_enabled(store,goal)
    assert_no_unmetered_worker(store,goal)
    assert budget_run_config(store,goal,'manager').tracing_disabled
    Budget.configure(store,goal,25,PRICES)
    with pytest.raises(BudgetError,match='unmetered'):
        assert_no_unmetered_worker(store,goal)
    with pytest.raises(ValueError,match='at most'):
        Budget.configure(store,goal,25.01,PRICES)


async def test_real_sdk_tool_turns_are_individually_reserved_and_measured(tmp_path,monkeypatch):
    store,goal,budget = configured(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY','fake-key-not-sent-to-network')
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['service_tier'] == 'default'
        assert body['max_output_tokens'] == 200
        # Reservation must be durable before the transport sees the request.
        assert store.db.execute("SELECT count(*) FROM dollar_requests WHERE state='reserved'").fetchone()[0] == 1
        output = ([{'type':'reasoning','id':'rs_sdk','summary':[], 'encrypted_content':'sdk-ciphertext'},
                   {'type':'function_call','id':'fc_test','call_id':'call_test','name':'read_fact','arguments':'{}','status':'completed'}]
            if len(calls)==1 else [{'type':'message','id':'msg_test','role':'assistant','status':'completed',
                                   'content':[{'type':'output_text','text':'Verified the fixture.','annotations':[]}]}])
        return httpx.Response(200,json={'id':f'resp_{len(calls)}','object':'response','created_at':1,
            'status':'completed','model':'test-model','output':output,
            'usage':{'input_tokens':30+len(calls),'output_tokens':10,
                     'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}})
    client = BudgetHTTPClient(budget,'manager',transport=httpx.MockTransport(handle))
    provider = BudgetModelProvider(budget,'manager',client=client)
    run_config = budget_run_config(store,goal,'manager',200)
    # Close unused real transport; all SDK traffic below uses the mock.
    await run_config.model_provider.aclose()
    run_config.model_provider = provider
    @function_tool
    async def read_fact() -> str:
        """Read a deterministic fixture fact."""
        return 'fixture fact'
    try:
        result = await Runner.run(Agent(name='test',model='test-model',tools=[read_fact]),
                                  'Read one fact and answer.',run_config=run_config,max_turns=3)
    finally:
        await provider.aclose()
    assert result.final_output == 'Verified the fixture.'
    assert len(calls) == 2
    rows = list(store.db.execute('SELECT * FROM dollar_requests ORDER BY created'))
    assert [row['response_id'] for row in rows] == ['resp_1','resp_2']
    assert all(row['state']=='settled' for row in rows)
    assert rows[1]['request_bytes'] > rows[0]['request_bytes']
    assert any(item.get('type')=='reasoning' and item.get('encrypted_content')=='sdk-ciphertext' for item in calls[1]['input'])
    assert budget.summary()['requests'] == 2


def test_encrypted_reasoning_requires_measured_same_goal_provenance_and_extra_bound(tmp_path):
    store,goal,budget = configured(tmp_path)
    reasoning = {'type':'reasoning','id':'rs_test','summary':[], 'encrypted_content':'ciphertext-generated-by-provider'}
    body = request_body(input=[reasoning])
    with pytest.raises(BudgetError,match='measured request'):
        budget.reserve(body,'manager')
    original = budget.reserve(request_body(),'research')
    budget.settle(original,{'id':'resp_reasoning','usage':{'input_tokens':30,'output_tokens':40},'output':[reasoning]})
    replay = budget.reserve(body,'manager')
    row = store.db.execute('SELECT * FROM dollar_requests WHERE id=?',(replay,)).fetchone()
    assert row['input_bound'] == len(body) + 4096 + 128 + 40
    other_goal = store.create_goal('Another run',[],tmp_path)
    other = Budget.configure(store,other_goal,25,PRICES)
    with pytest.raises(BudgetError,match='measured request'):
        other.reserve(body,'manager')


async def test_sdk_guard_http_diagnostic_propagates_without_retry(tmp_path,monkeypatch):
    store,goal,budget = configured(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(400,json={'error':{'type':'invalid_request_error','code':'unsupported_value',
            'param':'reasoning.effort','message':'Unsupported effort value'}})
    client = BudgetHTTPClient(budget,'manager',transport=httpx.MockTransport(reject))
    provider = BudgetModelProvider(budget,'manager',client=client)
    config = budget_run_config(store,goal,'manager')
    await config.model_provider.aclose()
    config.model_provider = provider
    try:
        with pytest.raises(BudgetError,match='reasoning.effort'):
            await Runner.run(Agent(name='test',model='test-model'),'Test',run_config=config)
    finally:
        await provider.aclose()
    assert len(calls) == 1
    row = store.db.execute('SELECT * FROM dollar_requests').fetchone()
    assert 'unsupported_value' in row['error']
    assert row['state'] == 'uncertain'


def test_custom_model_objects_cannot_bypass_guarded_provider(tmp_path):
    store,goal,budget = configured(tmp_path)
    with pytest.raises(BudgetError,match='priced model name'):
        budget_run_config(store,goal,'manager',model=object())


async def test_direct_legacy_worker_execution_is_blocked_before_attempt(tmp_path):
    from omnitrix.execution import Execution
    store,goal,budget = configured(tmp_path)
    class Worker:
        name = 'codex'
        async def run(self,*args):
            pytest.fail('Unmetered worker must not start')
    execution = Execution(store,Worker())
    task = execution.assign(goal,'Inspect fixture',1)
    with pytest.raises(BudgetError,match='unmetered'):
        await execution.send(task,'Inspect fixture',str(tmp_path))
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0


async def test_supervisor_persists_specific_budget_diagnostic_and_pauses(tmp_path):
    from omnitrix.supervisor import Supervisor
    store,goal,budget = configured(tmp_path)
    class Brain:
        async def decide(self,snapshot):
            raise BudgetError('Provider HTTP 400: unsupported reasoning.effort')
    supervisor = Supervisor(store,goal,brain=Brain(),runtime=object())
    result = await supervisor.advance(1)
    assert 'unsupported reasoning.effort' in result['blocked']['reason']
    assert store.snapshot(goal)['goal']['status'] == 'paused'
    event = store.db.execute("SELECT detail FROM activity WHERE kind='budget_guard_blocked'").fetchone()
    assert 'unsupported reasoning.effort' in event['detail']


def test_new_manager_and_chat_share_atomic_quarter_budget_across_restart(tmp_path):
    store,goal,budget = configured(tmp_path,.12)
    budget.reserve(request_body(),'manager')
    first = budget.summary()
    assert first['manager_limit_usd'] == .03
    other = Budget(Store(store.path),goal)
    with pytest.raises(BudgetError,match='sublimit'):
        other.reserve(request_body(),'manager_chat')
    assert other.summary()['committed_usd'] == first['committed_usd']
    assert other.summary()['remaining_usd'] > 0


def test_existing_budgets_receive_no_new_policy_on_reconfigure(tmp_path):
    store,goal,budget = configured(tmp_path,.12)
    store.execute('DELETE FROM dollar_budget_policies WHERE goal_id=?',(goal,))
    budget.reserve(request_body(),'manager')
    budget = Budget.configure(store,goal,.12,PRICES)
    assert budget.economical_policy() is None
    budget.reserve(request_body(),'manager_chat')
    assert budget.summary()['manager_limit_usd'] is None


def test_group_cap_counts_uncertain_requests_and_manager_bytes_fail_pre_reserve(tmp_path):
    store,goal,budget = configured(tmp_path)
    group = budget.request_group('manager',1)
    request = budget.reserve(request_body(),'manager',request_group=group)
    budget.uncertain(request,'transport failure')
    with pytest.raises(BudgetError,match='Per-decision'):
        Budget(Store(store.path),goal).reserve(request_body(),'manager',request_group=group)
    with pytest.raises(BudgetError,match='byte ceiling'):
        budget.reserve(request_body(input='x'*50000),'manager_chat')
    assert budget.summary()['requests'] == 1


def test_review_escrow_protects_admission_refunds_only_unused_and_never_refills(tmp_path):
    store,goal,budget = configured(tmp_path,.12)
    budget.admit_review('job',.1)
    budget.admit_review('job',.1)
    assert budget.summary()['protected_review_usd'] == .1
    with pytest.raises(BudgetError,match='exhausted'):
        budget.reserve(request_body(),'research')
    review = budget.reserve(request_body(),'evaluation',review_id='job')
    budget.settle(review,{'id':'resp_review','usage':{'input_tokens':20,'output_tokens':10}})
    assert budget.summary()['protected_review_usd'] == pytest.approx(.1-budget.summary()['committed_usd'])
    other = Budget(Store(store.path),goal)
    uncertain = other.reserve(request_body(),'evaluation',review_id='job')
    other.uncertain(uncertain,'cancelled')
    charged = other.summary()['committed_usd']
    other.release_review('job')
    assert other.summary()['protected_review_usd'] == 0
    assert other.summary()['committed_usd'] == charged
    with pytest.raises(BudgetError,match='reopened'):
        other.admit_review('job',.1)


def test_unsupported_reasoning_effort_rejected_before_client_or_reservation(tmp_path,monkeypatch):
    store,goal,_ = configured(tmp_path)
    monkeypatch.setenv('OMNITRIX_REASONING_EFFORT','none')
    with pytest.raises(BudgetError,match='requires reasoning effort'):
        budget_run_config(store,goal,'manager',model='gpt-6.1-sol')
    assert store.db.execute('SELECT count(*) FROM dollar_requests').fetchone()[0] == 0


def test_evaluation_request_cannot_exceed_escrow_or_refill_after_multiple_calls(tmp_path):
    store,goal,budget = configured(tmp_path)
    budget.admit_review('tiny',.001)
    with pytest.raises(BudgetError,match='protected review allowance'):
        budget.reserve(request_body(),'evaluation',review_id='tiny')
    assert budget.summary()['requests'] == 0
    budget.release_review('tiny')
    budget.admit_review('review',.1)
    total_actual = 0
    for index in range(3):
        request = budget.reserve(request_body(),'evaluation',review_id='review')
        budget.settle(request,{'id':f'resp_{index}','usage':{'input_tokens':20,'output_tokens':10}})
        total_actual += budget._cost('test-model',20,10)
        row = store.db.execute("SELECT * FROM dollar_review_escrows WHERE id='review'").fetchone()
        assert row['remaining_nano'] == row['amount_nano'] - total_actual
    with pytest.raises(BudgetError,match='original unresolved'):
        budget.settle(request,{'id':'repeat','usage':{'input_tokens':0,'output_tokens':0}})
    assert store.db.execute("SELECT remaining_nano FROM dollar_review_escrows WHERE id='review'").fetchone()[0] == 100_000_000-total_actual


def test_provider_payloads_survive_parser_failure(tmp_path):
    from omnitrix.store import Store
    from omnitrix.budget import Budget
    store=Store(tmp_path/'state.db')
    goal=store.create_goal('Retain provider output',['Keep evidence'],tmp_path)
    budget=Budget.configure(store,goal,25,{'model':{'input_per_million':5,'output_per_million':15}})
    request=json.dumps({'model':'model','input':'public source','max_output_tokens':100,'service_tier':'default'}).encode()
    rid=budget.reserve(request,'manager')
    response={'id':'resp-bad-json','usage':{'input_tokens':4,'output_tokens':8},'output':[{'type':'message','content':[{'type':'output_text','text':'malformed JSON'}]}]}
    budget.settle(rid,response)
    row=store.db.execute('SELECT request_json,response_json FROM dollar_payloads WHERE request_id=?',(rid,)).fetchone()
    assert json.loads(row[0])['input']=='public source'
    assert json.loads(row[1])['output'][0]['content'][0]['text']=='malformed JSON'


def test_uncertain_billing_is_distinct_from_new_request_eligibility(tmp_path):
    from omnitrix.store import Store
    from omnitrix.budget import Budget
    store=Store(tmp_path/'state.db');goal=store.create_goal('Research',['Retain evidence'],tmp_path)
    b=Budget.configure(store,goal,25,{'model':{'input_per_million':5,'output_per_million':15}})
    body=json.dumps({'model':'model','input':'source','service_tier':'default','max_output_tokens':100}).encode()
    first=b.reserve(body,'manager');b.uncertain(first,'Provider rejected old input')
    next_request=b.reserve(body,'manager');b.settle(next_request,{'id':'successful','usage':{'input_tokens':3,'output_tokens':2}})
    summary=b.summary()
    assert summary['unresolved_requests']==1 and summary['latest_request_state']=='settled'
    assert summary['remaining_usd']>0 and summary['last_settled_request_at']
    assert 'does not block' in summary['reservation_note']


def test_encrypted_reasoning_provenance_is_model_specific(tmp_path):
    from omnitrix.store import Store
    from omnitrix.budget import Budget,BudgetError
    store=Store(tmp_path/'state.db');goal=store.create_goal('Research',['Evidence'],tmp_path)
    prices={m:{'input_per_million':5,'output_per_million':15} for m in ['a','b']}
    budget=Budget.configure(store,goal,25,prices)
    first=budget.reserve(json.dumps({'model':'a','input':'text','max_output_tokens':100,'service_tier':'default'}).encode(),'research')
    budget.settle(first,{'id':'resp-a','usage':{'input_tokens':5,'output_tokens':5},'output':[{'type':'reasoning','encrypted_content':'opaque-a'}]})
    second=json.dumps({'model':'b','input':[{'type':'reasoning','encrypted_content':'opaque-a'}],'max_output_tokens':100,'service_tier':'default'}).encode()
    with pytest.raises(BudgetError,match='not produced'):
        budget.reserve(second,'evaluation')
