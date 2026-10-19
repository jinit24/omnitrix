#!/usr/bin/env python3
"""Build the public recorded-experiment view; never read a private database."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'demo-state.json').unlink(missing_ok=True)
    assets = ROOT / 'omnitrix/web'
    for name in ('app.js', 'styles.css'):
        shutil.copyfile(assets / name, output / name)
    html = (assets / 'index.html').read_text()
    html = html.replace('href="/', 'href="./').replace('src="/', 'src="./')
    html = html.replace('<body>', '<body data-state="experiment-state.json" data-static="true">')
    html = html.replace('Local environment', 'Recorded experiment').replace('Local workspace', 'Archived experiment')
    html = html.replace('Read-only</span>', 'Snapshot · read-only</span>')
    (output / 'index.html').write_text(html)
    docs = output / 'docs'
    docs.mkdir(exist_ok=True)
    for source in (ROOT / 'docs').rglob('*'):
        if not source.is_file() or source.suffix not in ('.html', '.css', '.js', '.json'):
            continue
        content = source.read_text()
        if source.suffix == '.html':
            content = content.replace('href="/"', 'href="' + '../' * (len(source.relative_to(ROOT / 'docs').parts)) + '"')
        target = docs / source.relative_to(ROOT / 'docs')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        # Keep historical flat JSON links working.
        if source.suffix == '.json':
            (docs / source.name).write_text(content)
    state = json.loads((ROOT / 'docs/experiments/manager-state.json').read_text())
    if not state.get('snapshot') or state.get('demo'):
        raise ValueError('Pages requires a recorded experiment snapshot')
    (output / 'experiment-state.json').write_text(json.dumps(state, indent=2) + '\n')
    (output / '.nojekyll').touch()
    return output



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.omnitrix/pages')
    args = parser.parse_args()
    print(build(args.output))
