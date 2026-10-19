"""Durable self-review scheduling for continuous, bounded research campaigns."""
import json
import time
from .control import Control

SCHEMA = '''CREATE TABLE IF NOT EXISTS campaign_cadence (
 goal_id TEXT PRIMARY KEY REFERENCES campaigns(goal_id), review_seconds INTEGER NOT NULL,
 wake_at REAL NOT NULL DEFAULT 0, cycle INTEGER NOT NULL DEFAULT 0,
 strategy INTEGER NOT NULL DEFAULT 0, failure_streak INTEGER NOT NULL DEFAULT 0,
 last_milestone_hypotheses INTEGER NOT NULL DEFAULT 0, last_review_decision INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '');'''


class Cadence:
    def __init__(self, store, goal_id):
        self.store, self.goal_id = store, goal_id

    def status(self):
        row = self.store.db.execute('SELECT * FROM campaign_cadence WHERE goal_id=?', (self.goal_id,)).fetchone()
        return dict(row) if row else None

    def configure(self, review_seconds):
        if not isinstance(review_seconds, int) or not 1 <= review_seconds <= 86400:
            raise ValueError('Review interval must be 1..86400 seconds')
        self.store.execute('INSERT INTO campaign_cadence(goal_id,review_seconds) VALUES(?,?)', (self.goal_id,review_seconds))

    def defer(self, reason, milestone=False, now=None, failed=True):
        current = self.status()
        if current is None:
            raise ValueError('Continuous review is not configured')
        now = time.time() if now is None else now
        hypotheses = self.store.db.execute("SELECT count(*) FROM research_jobs WHERE goal_id=? AND stage='research' AND status IN ('supported','falsified')", (self.goal_id,)).fetchone()[0]
        streak = 0 if milestone or not failed else current['failure_streak'] + 1
        delay = min(86400, current['review_seconds'] * 2 ** min(max(streak-1,0),5))
        deadline = self.store.db.execute('SELECT deadline FROM campaigns WHERE goal_id=?', (self.goal_id,)).fetchone()[0]
        with self.store.db:
            self.store.db.execute('''UPDATE campaign_cadence SET wake_at=?,cycle=cycle+1,
                strategy=strategy+?,failure_streak=?,last_milestone_hypotheses=?,reason=?,
                last_review_decision=(SELECT decisions_spent FROM campaigns WHERE goal_id=?) WHERE goal_id=?''',
                (min(now+delay,deadline),0 if milestone or not failed else 1,streak,
                 hypotheses if milestone else current['last_milestone_hypotheses'],reason,self.goal_id,self.goal_id))
            self.store.db.execute("UPDATE goals SET status='active' WHERE id=?", (self.goal_id,))
            self.store.db.execute('UPDATE campaigns SET stagnant_decisions=0 WHERE goal_id=?', (self.goal_id,))
        result = self.status()
        control = Control(self.store)
        control.event(self.goal_id, 'campaign_milestone' if milestone else 'campaign_self_review', result)
        control.message(self.goal_id, 'campaign_review', {
            'reason':reason,'strategy':result['strategy'],'next_wake_at':result['wake_at'],
            'next':'Choose a different unexamined component or falsifiable alternative. Review prior failed hypotheses and feedback; do not replay the same exhausted plan. Preserve negative results. No new issue should be invented just to stay busy.'},
            direction='worker_to_manager')
        return result

    def pace(self, now=None):
        """Spread finite spend over the saved horizon, admitting bounded bursts.

        The request reservation remains the hard ceiling. This only schedules
        between decisions, and never interrupts a role or refunds unknown costs.
        """
        from .spending import Spending
        now = time.time() if now is None else now
        budget = Spending(self.store,self.goal_id).status()
        campaign = self.store.db.execute('SELECT created,deadline FROM campaigns WHERE goal_id=?', (self.goal_id,)).fetchone()
        initial = min(.5, budget['limit_usd'] * .25)
        spent = budget['spent_usd'] + budget['reserved_usd']
        target = campaign['created'] + max(0, (spent-initial) / (budget['limit_usd']-initial)) * (campaign['deadline']-campaign['created'])
        target = min(campaign['deadline'], target)
        if target > now:
            self.store.execute('UPDATE campaign_cadence SET wake_at=max(wake_at,?),reason=? WHERE goal_id=?',
                (target,'Pace the remaining dollar budget across the saved campaign horizon',self.goal_id))
            Control(self.store).event(self.goal_id,'campaign_pacing',{'wake_at':target,'committed_usd':spent})
            return True
        return False

    def progress(self):
        # Re-reading the same excerpt or merely emitting tokens is not progress.
        sources = self.store.db.execute('''SELECT count(DISTINCT a.source_id) FROM research_access a
            JOIN research_runs r ON r.id=a.run_id JOIN research_jobs j ON j.id=r.job_id WHERE j.goal_id=?''', (self.goal_id,)).fetchone()[0]
        outcomes = self.store.db.execute("SELECT count(*) FROM research_jobs WHERE goal_id=? AND stage='research' AND status IN ('supported','falsified')", (self.goal_id,)).fetchone()[0]
        return sources + outcomes
