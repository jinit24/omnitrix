"""Admit a strategy batch before the deterministic queue spends research budget."""
from pydantic import TypeAdapter

from .campaign import Campaign
from .models import ScheduledResearch
from .research import Research


def enqueue_batch(store, goal_id, items, research=None, instructions=''):
    """Preflight every item, then persist all new plans/queue entries together.

    A saved strategy is not a worker start. Claiming and execution happen after
    this transaction, under the original role, attempt and request limits.
    """
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError('Schedule one to six bounded research plans')
    adapter = TypeAdapter(ScheduledResearch)
    entries = [adapter.validate_python(item) for item in items]
    service = research or Research(store)
    campaign = Campaign(store, goal_id)
    prepared = [(entry, service.preflight(goal_id, entry.stage, entry.plan)['spec'])
                for entry in entries]
    scheduled, skipped = [], []
    db = store.db
    db.execute('SAVEPOINT research_strategy_batch')
    try:
        for entry, spec in prepared:
            planned = service.plan(goal_id, entry.stage, spec)
            if planned['duplicate'] and planned['status'] != 'pending':
                skipped.append(planned)
                continue
            scheduled.append(campaign.enqueue(planned['job_id'], entry.priority,
                                               entry.instructions or instructions))
        db.execute('RELEASE research_strategy_batch')
    except BaseException:
        db.execute('ROLLBACK TO research_strategy_batch')
        db.execute('RELEASE research_strategy_batch')
        raise
    return {'scheduled': scheduled, 'skipped': skipped, 'campaign': campaign.summary()}
