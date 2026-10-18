"""Portable HTML research logs. Escape all model and source text."""
import html
import json
from datetime import datetime, timezone
from pathlib import Path


def render_report(data):
    def text(value):
        return html.escape(str(value), quote=True)

    def detail(value):
        return '<pre>' + text(json.dumps(value, indent=2, ensure_ascii=False)) + '</pre>'

    issues = ''.join('<article><h2>' + text(i.get('id', '')) + ': ' + text(i['title']) + '</h2><p class="status">' +
                     text(i['status']) + '</p>' + detail({k: v for k, v in i.items() if k not in ('id', 'title', 'status')}) + '</article>'
                     for i in data.get('issues', []))
    runs = ''.join('<details><summary>' + text(r.get('name', r.get('mode', 'Run'))) + '</summary>' + detail(r) + '</details>'
                   for r in data.get('runs', []))
    rows = []
    for run in data.get('runs', []):
        score = run.get('score', {})
        score_text = f"{score['passed']}/{score.get('total', '?')}" if isinstance(score, dict) and score.get('passed') is not None else 'Unscored'
        rows.append('<tr>' + ''.join('<td>' + text(value) + '</td>' for value in
                    (run.get('name', 'Run'), run.get('mode', ''), run.get('status', run.get('result', '')),
                     score_text, run.get('seconds', run.get('duration_seconds', '')))) + '</tr>')
    table = '<table><thead><tr><th>Experiment</th><th>Mode</th><th>Outcome</th><th>Checks</th><th>Seconds</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' if rows else ''
    extra = {k: v for k, v in data.items() if k not in ('title', 'issues', 'runs')}
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">' + \
        '<title>' + text(data.get('title', 'Research quality log')) + '</title><style>' + \
        'body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#1b2938;background:#f5f7fa}' + \
        'article,details{background:white;border:1px solid #d6dee8;border-radius:8px;padding:18px;margin:16px 0}' + \
        'h1,h2{line-height:1.25}h2{font-size:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}' + \
        'table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;border-bottom:1px solid #ccd6e3;padding:8px;overflow-wrap:anywhere}' + \
        '.status{font-weight:700;color:#245aa0}summary{cursor:pointer;font-weight:700}</style><body><h1>' + \
        text(data.get('title', 'Research quality log')) + '</h1>' + detail(extra) + issues + \
        '<h2>Experiment history</h2>' + table + runs + '</body></html>\n'


def write_report(data, output):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(data), encoding='utf-8')


def record_experiment(log_path, name, result):
    """Refresh an explicit shared log without discarding findings or previous runs."""
    path = Path(log_path)
    data = json.loads(path.read_text()) if path.exists() else {'title': 'Research quality log', 'issues': [], 'runs': []}
    summary = {'name': name} | {key: result[key] for key in
        ('mode', 'status', 'seconds', 'score', 'usage', 'reason', 'error_type', 'autonomous_discovery', 'verification', 'audits') if key in result}
    decisions = result.get('decisions', result.get('snapshot', {}).get('decisions', []))
    summary['decisions'] = [{'action': d.get('action'), 'rationale': d.get('rationale')} for d in decisions]
    implementations = result.get('snapshot', {}).get('implementations', [])
    if implementations:
        summary['implementations'] = [{'status': i['status'], 'candidate': i['candidate'], 'attempts': i['attempts']} for i in implementations]
    data['runs'] = [r for r in data.get('runs', []) if r.get('name') != name] + [summary]
    data['updated'] = datetime.now(timezone.utc).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    write_report(data, path.with_suffix('.html'))


def goal_report(store, goal_id):
    from .knowledge import Knowledge
    snapshot = store.snapshot(goal_id)
    return {'title': 'Omnitrix research evidence log', 'generated_at': datetime.now(timezone.utc).isoformat(),
            'goal': snapshot['goal'], 'research_progress': snapshot.get('research_progress', {}),
            'decisions': snapshot['decisions'], 'evidence': Knowledge(store).export(goal_id),
            'implementations': snapshot['implementations'],
            'verifications': [dict(r) for r in store.db.execute('SELECT * FROM verifications WHERE implementation_id IN (SELECT id FROM implementations WHERE goal_id=?)', (goal_id,))],
            'completion_audits': [dict(r) for r in store.db.execute('SELECT * FROM completion_audits WHERE goal_id=?', (goal_id,))],
            'runs': [{'name': 'Model usage', 'events': [json.loads(r[0]) for r in store.db.execute(
                "SELECT detail FROM activity WHERE goal_id=? AND kind='usage' ORDER BY created", (goal_id,))]}]}
