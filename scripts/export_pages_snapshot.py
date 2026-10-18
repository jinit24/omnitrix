#!/usr/bin/env python3
"""Explicitly export selected experiment state; omit sessions, paths and message bodies."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.dashboard import snapshot

FIELDS = {
    'tasks': 'id priority adapter status instructions',
    'runtime': 'task_id goal_revision policy_revision generation state spec started last_progress progress_count',
    'research_jobs': 'id stage spec status attempts created updated',
    'research_runs': 'id job_id status report evaluation feedback tool_calls created finished',
    'opportunities': 'id title spec score status created updated',
    'knowledge': 'id job_id kind title body classification citations created',
    'implementations': 'id authorization base_revision candidate status attempts feedback created',
    'verifications': 'id implementation_id candidate goal_revision requirement passed exit_code digest failure_kind created',
    'completion_audits': 'id revision candidate requirements passed created',
    'research_limits': 'max_runs spent',
    'policy_versions': 'version instructions created',
    'goal_versions': 'revision objective requirements constraints reason created',
}


def export(directory):
    goals, observed = [], []
    for task in ('deduplication', 'batch-cost', 'bounded-retries'):
        path = directory / 'trial-0' / task / 'manager/state.db'
        if not path.is_file():
            raise ValueError(f'Missing {task} state; refusing an incomplete export')
        for original in snapshot(path)['goals']:
            data = {table: [{k: row[k] for k in columns.split() if k in row} for row in original['data'][table]]
                    for table, columns in FIELDS.items()}
            data['decisions'] = [{'id': r['id'], 'created_at': r['created_at'], 'body': {
                k: r['body'][k] for k in ('action', 'rationale') if k in r['body']}}
                for r in original['data']['decisions']]
            data['activity'] = [{k: r[k] for k in ('id', 'kind', 'task_id', 'created')} | {'detail': {}}
                                for r in original['data']['activity']]
            data.update(messages=[], proposals=[])
            observed.extend(r['created'] for r in data['activity'])
            goal = {k: original[k] for k in ('id', 'objective', 'requirements', 'status', 'revision')}
            goals.append(goal | {'data': data, 'experiment_task': task})
    recorded = datetime.fromtimestamp(max(observed), timezone.utc).isoformat()
    report = ROOT / 'docs/experiments/live-api-v2-gpt54.json'
    return {'available': True, 'snapshot': True, 'recorded_at': recorded, 'goals': goals,
            'provenance': {'trial': directory.name, 'report': 'docs/experiments/live-api-v2-gpt54.json',
                           'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
                           'mode': 'real models on controlled fixtures'},
            'reason': 'Recorded real experiment · three manager runs on controlled fixtures. Archived results, not live workers or Kubernetes data.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/experiments/manager-state.json')
    args = parser.parse_args()
    payload = json.dumps(export(args.experiment), indent=2) + '\n'
    # Reject obvious accidental inclusion; only explicit projected fields are exported.
    import os
    key = os.getenv('OPENAI_API_KEY')
    if (key and key in payload) or any(p in payload for p in ('/workspace/', '/tmp/', 'sk-proj-')):
        raise ValueError('Export contains a credential or local path; review before publishing')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload)
    print(f'Exported {len(json.loads(payload)["goals"])} recorded goals')
