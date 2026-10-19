"""Portable HTML research logs. Escape all model and source text."""
import html
import json
import os
import tempfile
import fcntl
from functools import wraps
import uuid
from datetime import datetime, timezone
from pathlib import Path


def render_report(data):
    def text(value):
        return html.escape(str(value), quote=True)

    def detail(value):
        return '<pre>' + text(json.dumps(value, indent=2, ensure_ascii=False)) + '</pre>'

    def history(issue):
        return ''.join('<details><summary>' + text(h['at']) + ' · ' + text(h['event']) +
                       ' · ' + text(h.get('status', '')) + '</summary>' + detail(h['details']) + '</details>'
                       for h in issue.get('history', []))

    issues = ''.join('<article id="' + text(i.get('id', '')) + '"><h2>' + text(i.get('id', '')) + ': ' + text(i['title']) + '</h2><p class="status">' +
                     text(i['status']) + '</p>' + detail({k: v for k, v in i.items() if k not in ('id', 'title', 'status', 'history')}) + history(i) + '</article>'
                     for i in data.get('issues', []))
    issue_rows = ''.join('<tr><td><a href="#' + text(i.get('id', '')) + '">' + text(i.get('id', '')) +
                         '</a></td><td>' + text(i['title']) + '</td><td>' + text(i['status']) + '</td><td>' +
                         text(len(i.get('history', []))) + '</td></tr>' for i in data.get('issues', []))
    issue_table = '<table><thead><tr><th>Issue</th><th>Problem</th><th>Latest status</th><th>Dated updates</th></tr></thead><tbody>' + issue_rows + '</tbody></table>' if issue_rows else ''
    runs = ''.join('<details><summary>' + text(r.get('name', r.get('mode', 'Run'))) + '</summary>' + detail(r) + '</details>'
                   for r in data.get('runs', []))
    rows = []
    for run in data.get('runs', []):
        score = run.get('score', {})
        score_text = f"{score['passed']}/{score.get('total', '?')}" if isinstance(score, dict) and score.get('passed') is not None else 'Unscored'
        spending=run.get('spending') or {}
        cost=(f"${spending['spent_usd']:.4f} + ${spending.get('reserved_usd',0):.4f} held" if 'spent_usd' in spending else '')
        rows.append('<tr>' + ''.join('<td>' + text(value) + '</td>' for value in
                    (run.get('name', 'Run'), run.get('mode', ''), run.get('status', run.get('result', '')),
                     score_text, run.get('seconds', run.get('duration_seconds', '')), cost)) + '</tr>')
    table = '<table><thead><tr><th>Experiment</th><th>Mode</th><th>Outcome</th><th>Checks</th><th>Seconds</th><th>Model cost / uncertain holds</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' if rows else ''
    events = ''.join('<details><summary>' + text(e['at']) + ' · ' + text(e['result'].get('name', 'Run')) +
                     ' · ' + text(e['result'].get('status', '')) + '</summary>' + detail(e['result']) + '</details>'
                     for e in data.get('experiment_events', []))
    extra = {k: v for k, v in data.items() if k not in ('title', 'issues', 'runs', 'experiment_events')}
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">' + \
        '<title>' + text(data.get('title', 'Research quality log')) + '</title><style>' + \
        'body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#1b2938;background:#f5f7fa}' + \
        'article,details{background:white;border:1px solid #d6dee8;border-radius:8px;padding:18px;margin:16px 0}' + \
        'h1,h2{line-height:1.25}h2{font-size:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}' + \
        'table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;border-bottom:1px solid #ccd6e3;padding:8px;overflow-wrap:anywhere}' + \
        '.status{font-weight:700;color:#245aa0}summary{cursor:pointer;font-weight:700}</style><body><h1>' + \
        text(data.get('title', 'Research quality log')) + '</h1>' + detail(extra) + issue_table + issues + \
        '<h2>Latest experiment results</h2>' + table + runs + '<h2>Dated experiment history</h2>' + events + '</body></html>\n'


def write_report(data, output):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, render_report(data))


def _write_atomic(path, contents):
    # A reader sees either complete checkpoint, including if the writer crashes.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def serialized_log(function):
    @wraps(function)
    def update(log_path,*args,**kwargs):
        path=Path(log_path);path.parent.mkdir(parents=True,exist_ok=True)
        with Path(str(path)+'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return function(log_path,*args,**kwargs)
    return update


@serialized_log
def record_experiment(log_path, name, result):
    """Refresh an explicit shared log without discarding findings or previous runs."""
    path = Path(log_path)
    data = json.loads(path.read_text()) if path.exists() else {'title': 'Research quality log', 'issues': [], 'runs': []}
    summary = {'name': name} | {key: result[key] for key in
        ('mode', 'status', 'seconds', 'score', 'usage', 'reason', 'error_type', 'autonomous_discovery', 'verification', 'audits',
         'spending', 'frontier', 'quality', 'artifacts', 'head', 'architecture', 'model', 'variant',
         'budget_usd', 'time_limit_seconds', 'native_codex_cli') if key in result}
    decisions = result.get('decisions', result.get('snapshot', {}).get('decisions', []))
    summary['decisions'] = [{'action': d.get('action'), 'rationale': d.get('rationale')} for d in decisions]
    implementations = result.get('snapshot', {}).get('implementations', [])
    if implementations:
        summary['implementations'] = [{'status': i['status'], 'candidate': i['candidate'], 'attempts': i['attempts']} for i in implementations]
    data['runs'] = [r for r in data.get('runs', []) if r.get('name') != name] + [summary]
    data.setdefault('experiment_events', []).append({'id':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat(),'result':summary})
    data['updated'] = datetime.now(timezone.utc).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(data, indent=2) + '\n')
    write_report(data, path.with_suffix('.html'))


@serialized_log
def record_issue(log_path, issue_id, title, status, event, details):
    """Append a dated diagnosis/change/result; never erase the earlier issue history."""
    path=Path(log_path)
    data=json.loads(path.read_text()) if path.exists() else {'title':'Research quality log','issues':[],'runs':[]}
    issue=next((i for i in data['issues'] if i['id']==issue_id),None)
    if issue is None:
        issue={'id':issue_id,'title':title,'history':[]};data['issues'].append(issue)
    issue['status']=status
    issue.setdefault('history',[]).append({'at':datetime.now(timezone.utc).isoformat(),'status':status,'event':event,'details':details})
    data['updated']=datetime.now(timezone.utc).date().isoformat()
    path.parent.mkdir(parents=True,exist_ok=True)
    _write_atomic(path, json.dumps(data,indent=2)+'\n')
    write_report(data,path.with_suffix('.html'))


def goal_report(store, goal_id):
    from .knowledge import Knowledge
    from .campaign import Campaign
    from .frontier import Frontier
    snapshot = store.snapshot(goal_id, history_limit=None)
    return {'title': 'Omnitrix research evidence log', 'generated_at': datetime.now(timezone.utc).isoformat(),
            'goal': snapshot['goal'], 'research_progress': snapshot.get('research_progress', {}),
            'campaign': snapshot.get('campaign'), 'spending': snapshot.get('spending'),
            'frontier': Frontier(store,goal_id).summary(),
            'quality': Campaign(store,goal_id).quality() if snapshot.get('campaign') else None,
            'decisions': snapshot['decisions'], 'evidence': Knowledge(store).export(goal_id),
            'implementations': snapshot['implementations'],
            'verifications': [dict(r) for r in store.db.execute('SELECT * FROM verifications WHERE implementation_id IN (SELECT id FROM implementations WHERE goal_id=?)', (goal_id,))],
            'completion_audits': [dict(r) for r in store.db.execute('SELECT * FROM completion_audits WHERE goal_id=?', (goal_id,))],
            'campaign_events': [dict(r) for r in store.db.execute("SELECT kind,detail,created FROM activity WHERE goal_id=? AND kind IN ('campaign_stopped','campaign_resumed','campaign_checkpoint','read_only_recovery','decision_rejected','manager_budget_exhausted','manager_protocol_failure','manager_provider_failure','provider_request_failed','review_gate_rejected','review_gate_correction','research_allocation','investigator_handoff','investigator_plan_normalized','research_goal_audit','campaign_strategy_changed','human_repair_feedback','research_challenged','evidence_registered','manager_inventory','manager_frontier_query') ORDER BY id",(goal_id,))],
            'spending_requests': [dict(r) for r in store.db.execute('SELECT * FROM spending_requests WHERE goal_id=? ORDER BY created',(goal_id,))],
            'runs': [{'name': 'Model usage', 'events': [json.loads(r[0]) for r in store.db.execute(
                "SELECT detail FROM activity WHERE goal_id=? AND kind='usage' ORDER BY created", (goal_id,))]}]}
