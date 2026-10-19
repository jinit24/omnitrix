"""Dollars must be reserved before every request, including lost responses."""
from types import SimpleNamespace
import pytest

from omnitrix.spending import MeteredModel, Spending, SpendingLimitReached
from omnitrix.store import Store


def setup(tmp_path, dollars=.015):
    store = Store(tmp_path/'state.db')
    goal = store.create_goal('Budgeted test', ['Stay within budget'], tmp_path)
    spending = Spending(store, goal)
    spending.configure(dollars)
    return store, goal, spending


def usage(inputs=1000, cached=500, outputs=100):
    return SimpleNamespace(input_tokens=inputs, output_tokens=outputs,
                           input_tokens_details=SimpleNamespace(cached_tokens=cached))


def test_atomic_reservations_and_actual_cached_cost(tmp_path):
    store, goal, budget = setup(tmp_path)
    request = budget.reserve(1000,1000,'research')
    other = Spending(Store(store.path),goal)
    with pytest.raises(SpendingLimitReached):
        other.reserve(1000,1000,'manager')
    budget.settle(request,usage())
    assert budget.status()['spent_nanos'] == 2_050_000
    assert budget.status()['reserved_nanos'] == 0
    assert other.reserve(1000,1000,'manager')
    with pytest.raises(ValueError,match='settled'):
        budget.settle(request,usage())


def test_crash_cannot_release_unknown_cost_or_reset_budget(tmp_path):
    store, goal, budget = setup(tmp_path)
    budget.reserve(1000,1000,'research')
    store.db.close()
    restored = Spending(Store(tmp_path/'state.db'),goal)
    with pytest.raises(SpendingLimitReached):
        restored.reserve(1000,1000,'research')
    with pytest.raises(ValueError,match='reset'):
        restored.configure(25)
    assert restored.status()['reserved_nanos'] == 10_000_000


class FakeResponses:
    def __init__(self, budget, fail=False):
        self.budget, self.fail, self.created = budget, fail, []
        self.input_tokens = SimpleNamespace(count=self.count)

    async def count(self, **payload):
        assert payload['model'] == 'gpt-4.1-2025-04-14'
        return SimpleNamespace(input_tokens=1000)

    async def create(self, **payload):
        assert self.budget.status()['reserved_nanos'] > 0
        assert payload['service_tier'] == 'default'
        assert payload['max_output_tokens'] == 1000
        self.created.append(payload)
        if self.fail:
            raise ConnectionError('Response lost after remote inference may have started')
        return SimpleNamespace(usage=usage(),status='completed')


def model_for(store, goal, budget, fail=False):
    responses = FakeResponses(budget,fail)
    model = MeteredModel(store,goal,'test',SimpleNamespace(responses=responses))
    # Isolate accounting from SDK serialization; actual SDK integration is covered live.
    model._build_response_create_kwargs = lambda *a, **k: {'model':model.model,'input':'Evidence','tools':[], 'max_output_tokens':1000}
    return model,responses


async def test_provider_never_called_when_reservation_will_not_fit(tmp_path):
    store,goal,budget = setup(tmp_path,.005)
    model, client = model_for(store,goal,budget)
    with pytest.raises(SpendingLimitReached):
        await model._fetch_response()
    assert not client.created


async def test_response_loss_keeps_maximum_reservation(tmp_path):
    store,goal,budget = setup(tmp_path)
    model, client = model_for(store,goal,budget,fail=True)
    with pytest.raises(ConnectionError):
        await model._fetch_response()
    assert budget.status()['reserved_nanos'] == 10_000_000
    assert budget.status()['spent_nanos'] == 0
    with pytest.raises(SpendingLimitReached):
        await model._fetch_response()
    assert len(client.created) == 1


async def test_success_settles_actual_usage_and_releases_unused_reservation(tmp_path):
    store,goal,budget = setup(tmp_path)
    model, client = model_for(store,goal,budget)
    await model._fetch_response()
    assert budget.status()['spent_nanos'] == 2_050_000
    assert budget.status()['reserved_nanos'] == 0
    captured=store.db.execute('SELECT status,output FROM model_responses').fetchone()
    assert captured['status']=='completed' and captured['output']=='[]'


async def test_hosted_tools_and_hidden_history_cannot_bypass_cost_contract(tmp_path):
    store,goal,budget = setup(tmp_path)
    model, client = model_for(store,goal,budget)
    model._build_response_create_kwargs = lambda *a, **k: {'tools':[{'type':'web_search'}]}
    with pytest.raises(ValueError,match='Hosted'):
        await model._fetch_response()
    model._build_response_create_kwargs = lambda *a, **k: {'conversation':'mutable-history'}
    with pytest.raises(ValueError,match='hidden context'):
        await model._fetch_response()
    assert not client.created


@pytest.mark.parametrize('state,expired',[('paused',False),('running',True)])
async def test_pause_and_persisted_deadline_block_new_paid_requests(tmp_path,state,expired):
    import time
    store,goal,budget=setup(tmp_path)
    store.execute('''INSERT INTO campaigns(goal_id,created,deadline,max_decisions,max_research_runs,state,report_path)
        VALUES(?,?,?,?,?,?,?)''',(goal,time.time()-2,time.time()-1 if expired else time.time()+60,10,20,state,'unused.html'))
    model,client=model_for(store,goal,budget)
    with pytest.raises(SpendingLimitReached,match='stopped|deadline'):
        await model._fetch_response()
    assert not client.created
    assert budget.status()['reserved_nanos']==0


def test_codex_prices_are_reserved_and_cannot_be_switched_after_configuration(tmp_path):
    store=Store(tmp_path/'state.db');goal=store.create_goal('Codex control',['Use the same evidence'],tmp_path)
    budget=Spending(store,goal);budget.configure(1,'gpt-5.3-codex')
    request=budget.reserve(1000,1000,'single-codex')
    assert budget.status()['reserved_nanos']==15_750_000
    budget.settle(request,usage())
    assert budget.status()['spent_nanos']==2_362_500
    with pytest.raises(ValueError,match='preserved'):budget.configure(1)
    with pytest.raises(SpendingLimitReached,match='context'):budget.reserve(200001,100,'single-codex')


def test_mixed_model_requests_share_one_atomic_cap_and_keep_their_original_prices(tmp_path):
    store,goal,budget=setup(tmp_path,.02)
    first=budget.reserve(1000,1000,'reviewer')
    with pytest.raises(SpendingLimitReached):budget.reserve(1000,1000,'investigator','gpt-5.3-codex')
    budget.settle(first,usage())
    second=budget.reserve(1000,1000,'investigator','gpt-5.3-codex');budget.settle(second,usage())
    assert budget.status()['spent_nanos']==4_412_500
    assert budget.status()['limit_usd']==.02
    assert store.db.execute('SELECT model FROM spending_requests WHERE id=?',(second,)).fetchone()[0]=='gpt-5.3-codex'


def test_two_connections_cannot_settle_one_request_twice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store,goal,budget=setup(tmp_path,.02);request=budget.reserve(1000,1000,'research');barrier=Barrier(2)
    def settle():
        connection=Store(store.path);other=Spending(connection,goal);barrier.wait()
        try:other.settle(request,usage());return 'settled'
        except ValueError:return 'already settled'
        finally:connection.db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:settle(),range(2)))
    assert sorted(results)==['already settled','settled']
    assert budget.status()['spent_nanos']==2_050_000
    assert budget.status()['reserved_nanos']==0
