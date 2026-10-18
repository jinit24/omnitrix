#!/usr/bin/env python3
"""Build a public static demo from bundled assets; never read live state."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    assets = ROOT / 'omnitrix/web'
    for name in ('app.js', 'styles.css'):
        shutil.copyfile(assets / name, output / name)
    html = (assets / 'index.html').read_text()
    html = html.replace('href="/', 'href="./').replace('src="/', 'src="./')
    html = html.replace('<body>', '<body data-state="demo-state.json" data-static="true">')
    html = html.replace('Local environment', 'Public demo').replace('Local workspace', 'Sample workspace')
    html = html.replace('Read-only</span>', 'Demo · read-only</span>')
    (output / 'index.html').write_text(html)
    docs = output / 'docs'
    docs.mkdir(exist_ok=True)
    for source in (ROOT / 'docs').rglob('*'):
        if not source.is_file() or source.suffix not in ('.html', '.css', '.js', '.json'):
            continue
        content = source.read_text()
        if source.suffix == '.html':
            content = content.replace('href="/"', 'href="../"')
        (docs / source.name).write_text(content)
    (output / 'demo-state.json').write_text(json.dumps(demo(), indent=2) + '\n')
    (output / '.nojekyll').touch()
    return output


def demo():
    data = {k: [] for k in ('tasks', 'runtime', 'research_jobs', 'research_runs', 'implementations',
        'opportunities', 'activity', 'decisions', 'messages', 'knowledge', 'goal_versions',
        'policy_versions', 'research_limits', 'completion_audits', 'proposals', 'verifications')}
    data['tasks'] = [
        {'id': 'sample-discovery', 'instructions': 'Map the event-processing pipeline', 'adapter': 'discovery', 'status': 'completed', 'priority': 1},
        {'id': 'sample-research', 'instructions': 'Test whether batch size explains peak latency', 'adapter': 'research', 'status': 'running', 'priority': 1},
        {'id': 'sample-code', 'instructions': 'Implement only after research and user authorization', 'adapter': 'implementation', 'status': 'pending', 'priority': 2},
    ]
    data['decisions'] = [{'id': 1, 'body': {'action': 'research', 'rationale': 'Resolve the batching hypothesis before proposing code changes.'}}]
    data['policy_versions'] = [{'version': 1, 'instructions': 'Preserve delivery guarantees. Require independent executable checks.'}]
    data['research_limits'] = [{'spent': 2, 'max_runs': 12}]
    data['knowledge'] = [{'id': 'sample-assumption', 'title': 'Batch size may explain peak latency', 'classification': 'assumption', 'body': 'Illustrative hypothesis only. No system was inspected and no performance improvement is claimed.', 'citations': []}]
    goal = {'id': 'sample-goal', 'objective': 'Reduce event-processing latency without changing delivery guarantees',
        'requirements': ['Preserve ordering and delivery guarantees', 'Verify the improvement with executable checks'],
        'status': 'active', 'revision': 1, 'data': data}
    return {'available': True, 'demo': True, 'goals': [goal],
        'reason': 'Static demo · sample data only. No live manager, workers, or private database is connected.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.omnitrix/pages')
    args = parser.parse_args()
    print(build(args.output))
