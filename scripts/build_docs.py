#!/usr/bin/env python3
"""Render README and search index. Other guides are maintained HTML."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs'


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def build():
    outputs = {p.name: p.read_text() for p in sorted((ROOT / 'docs').glob('*.html')) if p.name != 'index.html'}
    # Share the layout with the maintained HTML guides, without another template.
    template = outputs['dashboard.html']
    readme = (ROOT / 'README.md').read_text()
    renderer = MarkdownIt('commonmark', {'html': False}).enable('table')
    article = renderer.render(readme).replace('href="docs/', 'href="')
    before, rest = template.split('<article>', 1)
    _, after = rest.split('</article>', 1)
    before = before.replace('Mission control · Omnitrix docs', 'Get started · Omnitrix docs')
    before = before.replace('<a class="selected" href="dashboard.html">', '<a class="" href="dashboard.html">')
    before = before.replace('<a class="" href="index.html">', '<a class="selected" href="index.html">')
    before = before[:before.index('<div class="kicker">')] + '<div class="kicker">FIELD GUIDE / 01</div><h1 class="page-title">Get started</h1>'
    after = after[:after.index('<aside class="outline">')] + '</div></body></html>'
    after = after.replace('Source: docs/dashboard.html', 'Source: README.md')
    outputs['index.html'] = before + '<article>' + article + '</article>' + after
    search = []
    for name, html in outputs.items():
        if not name.endswith('.html'):
            continue
        parser = PlainText()
        # Index the article, not repeated navigation.
        # Reports have multiple articles with IDs; index all issue histories.
        parser.feed(html.split('<article', 1)[1].split('>', 1)[1].rsplit('</article>', 1)[0])
        title = html.split('<title>', 1)[1].split('</title>', 1)[0].split(' · ', 1)[0]
        search.append({'title': title, 'href': name, 'text': ' '.join(parser.parts)})
    outputs['search.json'] = json.dumps(search, ensure_ascii=False, indent=2) + '\n'
    return {name: outputs[name] for name in ('index.html', 'search.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []
    outputs = build()
    for name, body in outputs.items():
        target = OUT / name
        if args.check:
            if not target.exists() or target.read_text() != body:
                failed.append(name)
        else:
            target.write_text(body)
    if failed:
        parser.exit(1, 'Outdated HTML docs: ' + ', '.join(failed) + '\n')
    print('HTML docs verified' if args.check else 'HTML index and search updated')
