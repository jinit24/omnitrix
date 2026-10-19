"""Reserve maximum request cost before inference; unknown outcomes stay charged.

Text-only, standard-tier model pricing is pinned per campaign. GPT-4.1 costs
$2/M input, $0.50/M cached input, $8/M output; GPT-5.4 costs $2.50/M input and
$15/M output (cached input charged at full rate conservatively). GPT-5.4 inputs
above 272K tokens, hosted tools and other models are excluded from this contract.
"""
import json
import time
import uuid
from decimal import Decimal

from openai import AsyncOpenAI, Omit
from agents.models.openai_responses import OpenAIResponsesModel

NANOS = 1_000_000_000
MODEL = 'gpt-4.1-2025-04-14'
INPUT_RATE, CACHED_RATE, OUTPUT_RATE = 2000, 500, 8000
# Nanodollars/token. GPT-5.4 cached inputs are conservatively charged at the
# full input rate; the saved dollar ceiling is an upper bound, not an invoice.
RATES = {MODEL: (2000, 500, 8000), 'gpt-5.4': (2500, 2500, 15000)}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS spending_limits (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), limit_nanos INTEGER NOT NULL,
 spent_nanos INTEGER NOT NULL DEFAULT 0, reserved_nanos INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS spending_requests (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id), role TEXT NOT NULL,
 reserved_nanos INTEGER NOT NULL, actual_nanos INTEGER, input_tokens INTEGER,
 output_tokens INTEGER, state TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS spending_goal ON spending_requests(goal_id,created);
'''


class SpendingLimitReached(RuntimeError):
    pass


class Spending:
    def __init__(self, store, goal_id):
        self.store, self.goal_id = store, goal_id

    def configure(self, dollars, model=MODEL):
        model = MODEL if model == 'gpt-4.1' else model
        if model not in RATES:
            raise ValueError('No verified campaign pricing contract for this model')
        amount = int(Decimal(str(dollars)) * NANOS)
        if amount <= 0:
            raise ValueError('A positive dollar limit is required')
        self.store.execute('INSERT OR IGNORE INTO spending_limits(goal_id,limit_nanos,model) VALUES(?,?,?)', (self.goal_id, amount, model))
        if self.status()['limit_nanos'] != amount or self.status()['model'] != model:
            raise ValueError('Existing dollar limit is preserved; starting again cannot reset or raise it')

    def status(self):
        row = self.store.db.execute('SELECT * FROM spending_limits WHERE goal_id=?', (self.goal_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result.update(limit_usd=result['limit_nanos']/NANOS, spent_usd=result['spent_nanos']/NANOS,
                      reserved_usd=result['reserved_nanos']/NANOS,
                      available_usd=(result['limit_nanos']-result['spent_nanos']-result['reserved_nanos'])/NANOS,
                      price_source='https://developers.openai.com/api/docs/models/'+('gpt-4.1' if result['model']==MODEL else result['model']))
        return result

    def reserve(self, input_tokens, max_output_tokens, role):
        if not isinstance(input_tokens,int) or input_tokens < 0 or not isinstance(max_output_tokens,int) or not 1 <= max_output_tokens <= 3500:
            raise ValueError('Invalid token reservation')
        # Counted by the provider on the exact request, with no mutable server history.
        model = self.status()['model']
        if model == 'gpt-5.4' and input_tokens > 272000:
            raise SpendingLimitReached('GPT-5.4 long-context pricing is outside the saved standard-context contract')
        input_rate, _, output_rate = RATES[model]
        amount = input_tokens * input_rate + max_output_tokens * output_rate
        request = uuid.uuid4().hex
        with self.store.db:
            campaign = self.store.db.execute('SELECT state,deadline FROM campaigns WHERE goal_id=?', (self.goal_id,)).fetchone()
            if campaign and (campaign['state'] != 'running' or campaign['deadline'] <= time.time()):
                raise SpendingLimitReached('Campaign is stopped or its saved deadline has expired')
            changed = self.store.db.execute('''UPDATE spending_limits SET reserved_nanos=reserved_nanos+?
                WHERE goal_id=? AND spent_nanos+reserved_nanos+?<=limit_nanos''', (amount, self.goal_id, amount))
            if changed.rowcount != 1:
                raise SpendingLimitReached('Remaining dollar budget cannot cover this request at its maximum cost')
            self.store.db.execute('INSERT INTO spending_requests VALUES(?,?,?,?,NULL,?,NULL,?,?)',
                (request, self.goal_id, role, amount, input_tokens, 'reserved', time.time()))
        return request

    def settle(self, request, usage):
        row = self.store.db.execute('SELECT * FROM spending_requests WHERE id=? AND goal_id=?', (request, self.goal_id)).fetchone()
        if not row or row['state'] != 'reserved':
            raise ValueError('Unknown or already settled request')
        cached = getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0
        input_rate, cached_rate, output_rate = RATES[self.status()['model']]
        cost = (usage.input_tokens - cached) * input_rate + cached * cached_rate + usage.output_tokens * output_rate
        if not 0 <= cached <= usage.input_tokens or cost > row['reserved_nanos']:
            raise SpendingLimitReached('Provider usage exceeded its token-count/output reservation; accounting requires review')
        with self.store.db:
            self.store.db.execute('UPDATE spending_limits SET reserved_nanos=reserved_nanos-?,spent_nanos=spent_nanos+? WHERE goal_id=?',
                                  (row['reserved_nanos'], cost, self.goal_id))
            self.store.db.execute("UPDATE spending_requests SET actual_nanos=?,input_tokens=?,output_tokens=?,state='settled' WHERE id=?",
                                  (cost, usage.input_tokens, usage.output_tokens, request))


class MeteredModel(OpenAIResponsesModel):
    def __init__(self, store, goal_id, role, client):
        super().__init__(Spending(store,goal_id).status()['model'], client)
        self.spending, self.role = Spending(store, goal_id), role

    async def _fetch_response(self, *args, **kwargs):
        payload = {k: v for k, v in self._build_response_create_kwargs(*args, **kwargs).items()
                   if not isinstance(v, Omit) and v is not None}
        if payload.get('stream') or any(payload.get(key) for key in
                ('conversation', 'previous_response_id', 'prompt', 'extra_body', 'extra_query', 'context_management')):
            raise ValueError('Budgeted research requires inline, nonstreaming text requests without hidden context')
        if any(t.get('type') != 'function' for t in payload.get('tools', [])):
            raise ValueError('Hosted/billable tools are disabled in budgeted research')
        def text_only(value):
            if isinstance(value, dict):
                if value.get('type') in ('input_image', 'input_file', 'input_audio', 'computer_call'):
                    raise ValueError('Only text evidence is supported by the campaign cost contract')
                for item in value.values():
                    text_only(item)
            elif isinstance(value, list):
                for item in value:
                    text_only(item)
        text_only(payload.get('input'))
        payload['max_output_tokens'] = min(payload.get('max_output_tokens', 3500), 3500)
        payload['service_tier'] = 'default'
        client = self._get_client()
        count_fields = {'model', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls', 'text', 'truncation'}
        counted = await client.responses.input_tokens.count(**{k: v for k, v in payload.items() if k in count_fields})
        request = self.spending.reserve(counted.input_tokens, payload['max_output_tokens'], self.role)
        # No automatic HTTP retries: a lost response might already have been billed.
        # On error/crash/cancellation the full reservation deliberately remains held.
        response = await client.responses.create(**payload)
        if response.usage is not None:
            self.spending.settle(request, response.usage)
        if response.status in ('failed', 'incomplete'):
            raise RuntimeError('Provider response did not complete; usage/reservation retained')
        return response


def metered_model(store, goal_id, role, fallback=None):
    if not store or not Spending(store, goal_id).status():
        return fallback or 'gpt-5.4'
    # An unspecified model follows the saved campaign's pinned pricing contract.
    # An explicit incompatible override must still fail before any paid call.
    model = Spending(store,goal_id).status()['model']
    requested = MODEL if fallback == 'gpt-4.1' else fallback
    if requested not in (None, model):
        raise ValueError('Campaign pricing currently supports only standard-tier '+('GPT-4.1' if model==MODEL else model)+' for this saved goal')
    if not hasattr(store, '_campaign_client'):
        store._campaign_client = AsyncOpenAI(base_url='https://api.openai.com/v1', max_retries=0, timeout=90)
    return MeteredModel(store, goal_id, role, store._campaign_client)
