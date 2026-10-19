import subprocess
import sys
import json

from omnitrix.reporting import render_report
from omnitrix.store import Store


def test_html_report_escapes_source_text():
    output = render_report({'title': '<script>alert(1)</script>', 'issues': [],
                            'runs': [{'name': '<img onerror=alert(1)>', 'evidence': '<script>alert(1)</script>'}]})
    assert '<script>alert(1)</script>' not in output
    assert '<img onerror=' not in output
    assert '&lt;script&gt;' in output


def test_report_cli_exports_a_real_goal_offline(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Inspect <source>', ['Read real evidence'], tmp_path)
    output = tmp_path / 'goal.html'
    subprocess.run([sys.executable, '-m', 'omnitrix.cli', '--db', str(store.path), 'report', goal, '--output', str(output)],
                   check=True, capture_output=True, text=True)
    html = output.read_text()
    assert 'Inspect &lt;source&gt;' in html and 'research_progress' in html


def test_record_experiment_preserves_findings_and_old_runs(tmp_path):
    from omnitrix.reporting import record_experiment
    path = tmp_path / 'quality.json'
    path.write_text(json.dumps({'title': 'Tracking', 'issues': [{'id': 'R1', 'title': 'A gap', 'status': 'open'}], 'runs': [{'name': 'baseline'}]}))
    record_experiment(path, 'trial-1', {'mode': 'live-autonomous', 'status': 'blocked', 'reason': 'API access absent'})
    record_experiment(path, 'trial-1', {'mode': 'live-autonomous', 'status': 'completed'})
    data = json.loads(path.read_text())
    assert data['issues'][0]['id'] == 'R1'
    assert [r['name'] for r in data['runs']] == ['baseline', 'trial-1']
    assert data['runs'][-1]['status'] == 'completed'
    assert [e['result']['status'] for e in data['experiment_events']]==['blocked','completed']
    assert path.with_suffix('.html').exists()


def test_issue_diagnosis_and_regression_history_is_append_only(tmp_path):
    from omnitrix.reporting import record_issue
    path=tmp_path/'quality.json'
    record_issue(path,'R1','Missing evidence','open','observed',{'run':'baseline','result':'No logs read'})
    record_issue(path,'R1','Missing evidence','fixed in tests','changed',{'fix':'Add log search'})
    record_issue(path,'R1','Missing evidence','open regression','live result',{'run':'live-2','result':'Still skips log tail'})
    issue=json.loads(path.read_text())['issues'][0]
    assert issue['status']=='open regression'
    assert [h['event'] for h in issue['history']]==['observed','changed','live result']
