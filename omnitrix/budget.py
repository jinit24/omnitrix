"""Durable pre-request dollar reservations for the paid Agents SDK path.

The guard sees the actual serialized Responses request, not a character estimate
of the initial prompt. Every tool turn reserves its own uncached input and maximum
output cost. Errors, cancellation and process death retain that full reservation.
Only provider-reported usage can release it. No budget means legacy behavior.
"""
import hashlib
import json
import os
import time
import uuid
from decimal import Decimal, ROUND_CEILING

from agents import Model, ModelProvider, ModelSettings, OpenAIProvider, RunConfig
from agents.model_settings import ModelRetrySettings
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
try:  # openai 2.x currently uses httpx2; older supported clients use httpx.
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on installed OpenAI SDK
    import httpx


SCHEMA = '''
CREATE TABLE IF NOT EXISTS dollar_budgets (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), limit_nano INTEGER NOT NULL,
 prices TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dollar_requests (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), role TEXT NOT NULL,
 model TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_bytes INTEGER NOT NULL,
 input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
 reserved_nano INTEGER NOT NULL, charged_nano INTEGER NOT NULL,
 state TEXT NOT NULL, response_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
 usage TEXT, error TEXT, created REAL NOT NULL, finished REAL);
CREATE INDEX IF NOT EXISTS dollar_requests_goal ON dollar_requests(goal_id);
CREATE TABLE IF NOT EXISTS dollar_budget_policies (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), manager_limit_nano INTEGER NOT NULL,
 manager_request_bytes INTEGER NOT NULL, manager_max_requests INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dollar_request_groups (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), role TEXT NOT NULL,
 max_requests INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dollar_request_group_members (
 request_id TEXT PRIMARY KEY REFERENCES dollar_requests(id), group_id TEXT NOT NULL REFERENCES dollar_request_groups(id));
CREATE TABLE IF NOT EXISTS dollar_review_escrows (
 goal_id TEXT NOT NULL REFERENCES goals(id), id TEXT NOT NULL, amount_nano INTEGER NOT NULL,
 remaining_nano INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(goal_id,id));
CREATE TABLE IF NOT EXISTS dollar_review_requests (
 request_id TEXT PRIMARY KEY REFERENCES dollar_requests(id), goal_id TEXT NOT NULL,
 escrow_id TEXT NOT NULL, consumed_nano INTEGER NOT NULL,
 FOREIGN KEY(goal_id,escrow_id) REFERENCES dollar_review_escrows(goal_id,id));
'''
NANO = Decimal(1_000_000_000)


class BudgetError(RuntimeError):
    """A paid operation cannot be safely issued under the configured budget."""


def _nano(amount):
    number = Decimal(str(amount))
    if not number.is_finite() or number < 0:
        raise ValueError('Dollar values must be finite and nonnegative')
    return int((number * NANO).to_integral_value(rounding=ROUND_CEILING))


def budget_enabled(store, goal_id):
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dollar_budgets'").fetchone()
    return bool(exists and store.db.execute('SELECT 1 FROM dollar_budgets WHERE goal_id=?', (goal_id,)).fetchone())


def assert_no_unmetered_worker(store, goal_id):
    if budget_enabled(store, goal_id):
        raise BudgetError('Dollar-budgeted goals cannot use unmetered Codex workers; use guarded research agents')


class Budget:
    def __init__(self, store, goal_id):
        self.store, self.goal_id = store, goal_id
        if not budget_enabled(store, goal_id):
            raise BudgetError('No dollar budget configured for this goal')
        store.execute("CREATE TABLE IF NOT EXISTS dollar_payloads (request_id TEXT PRIMARY KEY REFERENCES dollar_requests(id), request_json TEXT, response_json TEXT)")

    @classmethod
    def configure(cls, store, goal_id, limit_usd=25, prices=None, *, manager_fraction=.25,
                  manager_request_bytes=48*1024, manager_max_requests=3):
        """Activate an immutable budget. Resuming a run never resets expenditure.

        Prices are explicit conservative dollars per million tokens. Configure
        the most expensive applicable context tier and omit cache discounts.
        """
        limit = _nano(limit_usd)
        if not 0 < limit <= _nano(25):
            raise ValueError('A run dollar limit must be greater than zero and at most $25')
        if not prices:
            raise ValueError('Explicit model prices are required')
        fraction = Decimal(str(manager_fraction))
        if not fraction.is_finite() or not 0 < fraction <= 1:
            raise ValueError('Manager fraction must be greater than zero and at most one')
        if type(manager_request_bytes) is not int or manager_request_bytes < 4096 or type(manager_max_requests) is not int or manager_max_requests < 1:
            raise ValueError('Manager request limits require at least 4096 bytes and one request')
        normalized = {}
        for model, price in prices.items():
            if not isinstance(model, str) or not model:
                raise ValueError('Each price must name an exact model')
            normalized[model] = {'input_nano_per_million': _nano(price['input_per_million']),
                                 'output_nano_per_million': _nano(price['output_per_million'])}
            if not all(normalized[model].values()):
                raise ValueError('Model prices must be positive')
        if store.db.in_transaction:
            raise BudgetError('Configure dollar budget outside an existing transaction')
        store.db.executescript(SCHEMA)
        if not store.db.execute('SELECT 1 FROM goals WHERE id=?', (goal_id,)).fetchone():
            raise ValueError('Unknown goal')
        prices_json = json.dumps(normalized, sort_keys=True)
        with store.db:
            inserted = store.db.execute('INSERT OR IGNORE INTO dollar_budgets VALUES(?,?,?,?)',
                             (goal_id, limit, prices_json, time.time()))
            if inserted.rowcount:
                store.db.execute('INSERT INTO dollar_budget_policies VALUES(?,?,?,?)',
                    (goal_id,int(Decimal(limit)*fraction),manager_request_bytes,manager_max_requests))
            current = store.db.execute('SELECT * FROM dollar_budgets WHERE goal_id=?', (goal_id,)).fetchone()
            if current['limit_nano'] != limit or current['prices'] != prices_json:
                raise BudgetError('Existing run budget and prices are immutable; create a new goal for a new run')
        return cls(store, goal_id)

    def economical_policy(self):
        exists = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dollar_budget_policies'").fetchone()
        row = self.store.db.execute('SELECT * FROM dollar_budget_policies WHERE goal_id=?',(self.goal_id,)).fetchone() if exists else None
        return dict(row) if row else None

    def _review_held(self):
        if self.economical_policy() is None:
            return 0
        return self.store.db.execute("SELECT coalesce(sum(remaining_nano),0) FROM dollar_review_escrows WHERE goal_id=? AND state='active'",(self.goal_id,)).fetchone()[0]

    def admit_review(self, package_id, amount_usd=.75):
        """Protect an explicit per-review envelope before research.

        The compatibility default is an initial allowance, not a guarantee of
        sufficient evaluation work. Research plans should pass their configured
        review_budget_usd. The same package never refills or reopens.
        """
        amount = _nano(amount_usd)
        if not self.economical_policy() or not isinstance(package_id,str) or not package_id or amount <= 0:
            raise BudgetError('Review escrow requires a new economical budget, package ID and positive allowance')
        db = self.store.db
        if db.in_transaction:
            raise BudgetError('Review admission requires a committed database')
        try:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM dollar_review_escrows WHERE goal_id=? AND id=?',(self.goal_id,package_id)).fetchone()
            if existing:
                if existing['amount_nano'] != amount or existing['state'] != 'active':
                    raise BudgetError('Review escrow cannot be changed or reopened')
            else:
                charged = db.execute('SELECT coalesce(sum(charged_nano),0) FROM dollar_requests WHERE goal_id=?',(self.goal_id,)).fetchone()[0]
                if charged + self._review_held() + amount > self._config()['limit_nano']:
                    raise BudgetError('Insufficient uncommitted budget to protect independent evaluation')
                db.execute("INSERT INTO dollar_review_escrows VALUES(?,?,?,?,?,?)",
                    (self.goal_id,package_id,amount,amount,'active',time.time()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return {'id':package_id,'protected_usd':amount/1e9}

    def release_review(self, package_id):
        """Release unspent escrow only; request charges and uncertainty survive."""
        self.store.execute("UPDATE dollar_review_escrows SET remaining_nano=0,state='released' WHERE goal_id=? AND id=? AND state='active'",(self.goal_id,package_id))

    def request_group(self, role, max_requests):
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError('Request group cap must be a positive integer')
        group_id = uuid.uuid4().hex
        self.store.execute('INSERT INTO dollar_request_groups VALUES(?,?,?,?,?)',
                           (group_id,self.goal_id,role,max_requests,time.time()))
        return group_id

    def _config(self):
        return self.store.db.execute('SELECT * FROM dollar_budgets WHERE goal_id=?', (self.goal_id,)).fetchone()

    def price(self, model):
        price = json.loads(self._config()['prices']).get(model)
        if price is None:
            raise BudgetError(f'No configured dollar price for model {model!r}')
        return price

    def _cost(self, model, input_tokens, output_tokens):
        price = self.price(model)
        numerator = (input_tokens * price['input_nano_per_million'] +
                     output_tokens * price['output_nano_per_million'])
        return (numerator + 999_999) // 1_000_000

    def reserve(self, body, role, *, request_group=None, review_id=None):
        """Reserve before network using exact JSON request bytes, never raw text logs."""
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            raise BudgetError('Budget guard requires a serialized JSON request') from None
        if not isinstance(payload, dict):
            raise BudgetError('Budget guard requires a Responses request object')
        model = payload.get('model')
        self.price(model)
        policy = self.economical_policy()
        if policy and role in ('manager','manager_chat') and len(body) > policy['manager_request_bytes']:
            raise BudgetError('Manager serialized request exceeds its byte ceiling; compact context before another call')
        effort = (payload.get('reasoning') or {}).get('effort')
        if model.startswith('gpt-6.1') and effort not in (None,'low','medium','high','xhigh','max'):
            raise BudgetError('This model requires reasoning effort low, medium, high, xhigh or max')
        if payload.get('service_tier') != 'default':
            raise BudgetError('Dollar-budgeted requests require standard service_tier=default')
        if (payload.get('reasoning') or {}).get('mode') not in (None, 'standard'):
            raise BudgetError('Separately priced reasoning modes are not budgeted')
        output_bound = payload.get('max_output_tokens')
        if type(output_bound) is not int or output_bound < 1:
            raise BudgetError('Each paid request requires an explicit maximum output token count')
        if payload.get('stream') or any(payload.get(key) for key in ('previous_response_id', 'conversation', 'prompt', 'background', 'context_management')):
            raise BudgetError('Streaming, server-side history, stored prompts and background calls are not budgeted')
        if any(tool.get('type') != 'function' for tool in payload.get('tools', [])):
            raise BudgetError('Hosted tools have additional charges and are not budgeted')
        def text_content(value):
            if isinstance(value, str):
                return
            if not isinstance(value, list):
                raise BudgetError('Only self-contained text input is supported by the dollar guard')
            for content in value:
                if not isinstance(content, dict) or content.get('type') not in ('input_text','output_text','text','refusal'):
                    raise BudgetError('Opaque, media and unknown content types are not budgeted')
                if not isinstance(content.get('text', content.get('refusal')), str):
                    raise BudgetError('Text input content must include its complete text')
        # Encrypted reasoning is opaque. Its byte length alone cannot bound
        # provider-side token expansion. Accept only content returned by a
        # measured request in this same goal, and additionally reserve that
        # response's entire output token count for every replayed item.
        reasoning_provenance = {}
        for prior in self.store.db.execute("SELECT usage FROM dollar_requests WHERE goal_id=? AND model=? AND state='settled'", (self.goal_id,model)):
            for item in json.loads(prior['usage'] or '{}').get('_budget_reasoning', []):
                reasoning_provenance[item['sha256']] = item['token_bound']
        reasoning_bound = 0
        items = payload.get('input')
        if isinstance(items, str):
            pass
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    raise BudgetError('Only self-contained text input items are budgeted')
                kind = item.get('type', 'message' if item.get('role') else None)
                if kind == 'message' and item.get('role') in ('user','assistant','system','developer'):
                    text_content(item.get('content'))
                elif kind == 'function_call':
                    if not all(isinstance(item.get(key),str) for key in ('name','arguments','call_id')):
                        raise BudgetError('Function calls must include complete identity and arguments')
                elif kind == 'function_call_output':
                    if not isinstance(item.get('call_id'),str):
                        raise BudgetError('Function results require their call ID')
                    text_content(item.get('output'))
                elif kind == 'reasoning':
                    encrypted = item.get('encrypted_content')
                    if not isinstance(encrypted,str) or not encrypted:
                        raise BudgetError('Reasoning input requires self-contained encrypted content with measured provenance')
                    digest = hashlib.sha256(encrypted.encode()).hexdigest()
                    if digest not in reasoning_provenance:
                        raise BudgetError('Encrypted reasoning was not produced by a measured request in this goal')
                    reasoning_bound += reasoning_provenance[digest]
                else:
                    raise BudgetError('item_reference and unknown input item types are not budgeted')
        else:
            raise BudgetError('Paid request input must contain complete text')
        # UTF-8 bytes upper-bound text tokens (one byte per token minimum),
        # including serialized schemas, tools and history. Additional framing
        # allowance covers protocol tokens that are not visible in JSON.
        items = payload.get('input', [])
        count = len(items) if isinstance(items, list) else 1
        input_bound = len(body) + 4096 + 128 * (count + len(payload.get('tools', []))) + reasoning_bound
        amount = self._cost(model, input_bound, output_bound)
        request_id = uuid.uuid4().hex
        db = self.store.db
        if db.in_transaction:
            raise BudgetError('Paid request cannot start inside an uncommitted database transaction')
        try:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM dollar_requests WHERE goal_id=? AND state='overrun'", (self.goal_id,)).fetchone():
                raise BudgetError('A prior request exceeded provider bounds; billing inspection is required')
            spent = db.execute('SELECT COALESCE(sum(charged_nano),0) FROM dollar_requests WHERE goal_id=?', (self.goal_id,)).fetchone()[0]
            if policy and role in ('manager','manager_chat'):
                manager_spent = db.execute("SELECT coalesce(sum(charged_nano),0) FROM dollar_requests WHERE goal_id=? AND role IN ('manager','manager_chat')",(self.goal_id,)).fetchone()[0]
                if manager_spent + amount > policy['manager_limit_nano']:
                    raise BudgetError('Manager and chat dollar sublimit exhausted; researcher and review funds remain protected')
            if request_group:
                group = db.execute('SELECT * FROM dollar_request_groups WHERE id=? AND goal_id=? AND role=?',(request_group,self.goal_id,role)).fetchone()
                count = db.execute('SELECT count(*) FROM dollar_request_group_members WHERE group_id=?',(request_group,)).fetchone()[0]
                if not group or count >= group['max_requests']:
                    raise BudgetError('Per-decision model request cap exhausted; retain state and change the approach')
            consumed = 0
            if review_id:
                escrow = db.execute("SELECT * FROM dollar_review_escrows WHERE goal_id=? AND id=? AND state='active'",(self.goal_id,review_id)).fetchone()
                if role != 'evaluation' or not escrow or escrow['remaining_nano'] < amount:
                    raise BudgetError('Evaluation request exceeds its protected review allowance')
                consumed = amount
            if spent + amount + self._review_held() - consumed > self._config()['limit_nano']:
                raise BudgetError(f'Dollar budget exhausted: request needs ${amount / 1e9:.6f}, remaining ${(self._config()["limit_nano"] - spent) / 1e9:.6f}')
            db.execute('''INSERT INTO dollar_requests
                (id,goal_id,role,model,request_sha256,request_bytes,input_bound,output_bound,
                 reserved_nano,charged_nano,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (request_id,self.goal_id,role,model,hashlib.sha256(body).hexdigest(),len(body),
                 input_bound,output_bound,amount,amount,'reserved',time.time()))
            db.execute('INSERT INTO dollar_payloads(request_id,request_json) VALUES(?,?)', (request_id,body.decode('utf-8')))
            if request_group:
                db.execute('INSERT INTO dollar_request_group_members VALUES(?,?)',(request_id,request_group))
            if consumed:
                db.execute('UPDATE dollar_review_escrows SET remaining_nano=remaining_nano-? WHERE goal_id=? AND id=?',(consumed,self.goal_id,review_id))
                db.execute('INSERT INTO dollar_review_requests VALUES(?,?,?,?)',(request_id,self.goal_id,review_id,consumed))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return request_id

    def settle(self, request_id, payload):
        db = self.store.db
        if db.in_transaction:
            raise BudgetError('Settle measured usage outside an existing database transaction')
        db.execute('BEGIN IMMEDIATE')
        try:
            self._settle_locked(request_id,payload)
        except BudgetError:
            # Missing usage and provider overruns are durable outcomes too.
            db.commit()
            raise
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()

    def _settle_locked(self, request_id, payload):
        row = self.store.db.execute('SELECT * FROM dollar_requests WHERE id=? AND goal_id=?', (request_id,self.goal_id)).fetchone()
        if row is None or row['state'] != 'reserved':
            raise BudgetError('Only the original unresolved request can be settled')
        self.store.execute('UPDATE dollar_payloads SET response_json=? WHERE request_id=?', (json.dumps(payload), request_id))
        usage = payload.get('usage') or {}
        incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
        response_id = payload.get('id')
        if not isinstance(response_id, str) or not response_id or type(incoming) is not int or type(outgoing) is not int or min(incoming,outgoing) < 0:
            self.uncertain(request_id, 'Provider response lacks response ID or valid measured token usage')
            raise BudgetError('Provider usage missing; full reservation retained')
        amount = self._cost(row['model'], incoming, outgoing)
        usage = dict(usage)
        usage['_budget_reasoning'] = [
            {'sha256':hashlib.sha256(item['encrypted_content'].encode()).hexdigest(), 'token_bound':outgoing}
            for item in payload.get('output', []) if isinstance(item,dict) and item.get('type') == 'reasoning'
            and isinstance(item.get('encrypted_content'),str) and item['encrypted_content']]
        if incoming > row['input_bound'] or outgoing > row['output_bound'] or amount > row['reserved_nano']:
            # Keep actual liability visible and stop all future requests. This
            # indicates a violated provider contract, not permission to continue.
            self.store.execute("UPDATE dollar_requests SET state='overrun',charged_nano=?,response_id=?,input_tokens=?,output_tokens=?,usage=?,error=?,finished=? WHERE id=?",
                (max(amount,row['reserved_nano']),response_id,incoming,outgoing,json.dumps(usage),
                 'Provider token usage exceeded conservative request bounds',time.time(),request_id))
            raise BudgetError('Provider usage exceeded reserved bounds; stop and inspect billing')
        self.store.db.execute("UPDATE dollar_requests SET state='settled',charged_nano=?,response_id=?,input_tokens=?,output_tokens=?,usage=?,finished=? WHERE id=?",
            (amount,response_id,incoming,outgoing,json.dumps(usage),time.time(),request_id))
        if self.economical_policy():
            escrow = self.store.db.execute('SELECT * FROM dollar_review_requests WHERE request_id=?',(request_id,)).fetchone()
            if escrow:
                self.store.db.execute("UPDATE dollar_review_escrows SET remaining_nano=remaining_nano+? WHERE goal_id=? AND id=? AND state='active'",
                    (max(0,escrow['consumed_nano']-amount),self.goal_id,escrow['escrow_id']))

    def uncertain(self, request_id, error):
        self.store.execute("UPDATE dollar_requests SET state='uncertain',error=?,finished=? WHERE id=? AND state='reserved'",
                           (str(error)[:1000],time.time(),request_id))

    def summary(self):
        rows = self.store.db.execute('SELECT * FROM dollar_requests WHERE goal_id=? ORDER BY created', (self.goal_id,)).fetchall()
        committed = sum(row['charged_nano'] for row in rows)
        actual = sum(row['charged_nano'] for row in rows if row['state'] in ('settled','overrun'))
        role_spend = {}
        for row in rows:
            role_spend[row['role']] = role_spend.get(row['role'],0) + row['charged_nano']
        policy = self.economical_policy()
        manager_spend = sum(role_spend.get(role,0) for role in ('manager','manager_chat'))
        held = self._review_held()
        return {'limit_usd': self._config()['limit_nano']/1e9, 'committed_usd': committed/1e9,
                'role_committed_usd':{role:value/1e9 for role,value in role_spend.items()},
                'manager_limit_usd':policy['manager_limit_nano']/1e9 if policy else None,
                'manager_remaining_usd':max(0,(policy['manager_limit_nano']-manager_spend)/1e9) if policy else None,
                'protected_review_usd':held/1e9,
                'available_research_usd':max(0,(self._config()['limit_nano']-committed-held)/1e9),
                'actual_usd':actual/1e9, 'reserved_usd':(committed-actual)/1e9,
                'measured_usage_usd':actual/1e9,
                'cost_basis':'Conservative configured rates applied to provider token usage; not an invoice',
                'remaining_usd':max(0,(self._config()['limit_nano']-committed)/1e9),
                'requests':len(rows), 'unresolved_requests':sum(row['state'] in ('reserved','uncertain') for row in rows),
                'latest_request_state':rows[-1]['state'] if rows else None,
                'last_settled_request_at':max((r['finished'] for r in rows if r['state']=='settled'),default=None),
                'reservation_note':'Uncertain request costs remain fully reserved. Billing uncertainty alone does not block further guarded read-only research within remaining funds; reservations need not be released to continue.'}


class BudgetHTTPClient(httpx.AsyncClient):
    """Guard the final serialized request immediately before HTTP transmission."""
    def __init__(self, budget, role, *, request_group=None, review_id=None, **kwargs):
        self.budget, self.role = budget, role
        self.request_group, self.review_id = request_group, review_id
        kwargs['follow_redirects'] = False
        super().__init__(**kwargs)

    async def send(self, request, **kwargs):
        if request.method != 'POST' or request.url.path.rstrip('/').split('/')[-1] != 'responses':
            raise BudgetError('Budgeted SDK client only permits guarded Responses calls')
        if kwargs.get('follow_redirects') is True:
            raise BudgetError('Budgeted requests cannot follow redirects')
        body = await request.aread()
        request_id = self.budget.reserve(body, self.role, request_group=self.request_group, review_id=self.review_id)
        try:
            response = await super().send(request, **kwargs)
            await response.aread()
            if not 200 <= response.status_code < 300:
                try:
                    detail = response.json().get('error', {})
                    diagnostic = {key:str(detail[key])[:1500] for key in ('type','code','param','message') if detail.get(key) is not None}
                except (ValueError,AttributeError,TypeError):
                    diagnostic = {'message':'Provider returned a non-JSON error'}
                raise BudgetError(f'Provider HTTP {response.status_code}: {json.dumps(diagnostic,sort_keys=True)}; outcome uncertain and reservation retained')
            self.budget.settle(request_id, response.json())
            return response
        except BaseException as exc:
            self.budget.uncertain(request_id, type(exc).__name__ + ': ' + str(exc))
            raise


class BudgetModelProvider(ModelProvider):
    """Every SDK model/tool turn uses one guarded, retry-free HTTP client."""
    def __init__(self, budget, role, *, client=None):
        self.budget = budget
        if client is not None and (not isinstance(client, BudgetHTTPClient) or client.budget.goal_id != budget.goal_id):
            raise BudgetError('Budget model provider requires its own guarded HTTP client')
        self.http_client = client or BudgetHTTPClient(budget, role, timeout=120)
        self.provider = OpenAIProvider(openai_client=AsyncOpenAI(http_client=self.http_client,max_retries=0),
                                       use_responses=True,use_responses_websocket=False)

    def get_model(self, model_name):
        self.budget.price(model_name)
        return BudgetModel(self.provider.get_model(model_name))

    async def aclose(self):
        await self.http_client.aclose()


class BudgetModel(Model):
    """Keep guard failures recognizable after OpenAI's transport error wrapping."""
    def __init__(self, inner):
        self.inner = inner

    async def get_response(self, *args, **kwargs):
        try:
            return await self.inner.get_response(*args, **kwargs)
        except Exception as exc:
            cause = exc
            visited = set()
            while cause is not None and id(cause) not in visited:
                if isinstance(cause, BudgetError):
                    raise cause from None
                visited.add(id(cause))
                cause = cause.__cause__
            raise

    async def stream_response(self, *args, **kwargs):
        raise BudgetError('Streaming model requests are not supported under a dollar budget')
        yield  # pragma: no cover - makes this an async iterator

    async def _cleanup_on_run_end(self, owner):
        await self.inner._cleanup_on_run_end(owner)

    async def close(self):
        await self.inner.close()


def budget_run_config(store, goal_id, role, max_output_tokens=3500, *, model=None,
                      max_requests=None, review_id=None):
    if store is None or not budget_enabled(store, goal_id):
        return RunConfig(tracing_disabled=True)
    if model is not None and not isinstance(model,str):
        raise BudgetError('Budgeted agents must select a priced model name through the guarded provider')
    effort = os.getenv('OMNITRIX_REASONING_EFFORT')
    if model and model.startswith('gpt-6.1') and effort not in (None,'low','medium','high','xhigh','max'):
        raise BudgetError('This model requires reasoning effort low, medium, high, xhigh or max')
    budget = Budget(store,goal_id)
    policy = budget.economical_policy()
    if policy and role in ('manager','manager_chat'):
        max_requests = min(max_requests,policy['manager_max_requests']) if max_requests is not None else policy['manager_max_requests']
        max_output_tokens = min(max_output_tokens,2000)
    group = budget.request_group(role,max_requests) if max_requests else None
    client = BudgetHTTPClient(budget,role,timeout=120,request_group=group,review_id=review_id)
    return RunConfig(tracing_disabled=True,
        model_provider=BudgetModelProvider(budget,role,client=client),
        model_settings=ModelSettings(max_tokens=max_output_tokens, store=False,
            response_include=['reasoning.encrypted_content'],
            reasoning=Reasoning(effort=os.environ['OMNITRIX_REASONING_EFFORT']) if os.getenv('OMNITRIX_REASONING_EFFORT') else None,
            retry=ModelRetrySettings(max_retries=0), extra_args={'service_tier':'default'}))


async def close_budget_run_config(run_config):
    if isinstance(run_config.model_provider, BudgetModelProvider):
        await run_config.model_provider.aclose()
