#!/usr/bin/env python3
"""Read-only, restartable public-repository research experiment.

Preparation never makes model calls. The live arm uses the normal Supervisor,
AgentBrain and ResearchAgents, and rejects every code-execution action in Python.
"""
import argparse
from contextlib import closing
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omnitrix.control import Control
from omnitrix.knowledge import Knowledge
from omnitrix.manager import AgentBrain
from omnitrix.research import Research, ResearchAgents
from omnitrix.store import Store
from omnitrix.supervisor import Supervisor

MAX_SOURCE_BYTES = 128 * 1024
DEFAULT_MODEL = 'gpt-6.1-sol'
# Upper bounds, including long-context rates and regional surcharge. These are
# conservative estimates of API cost, not an invoice or measured economic value.
DEFAULT_PRICES = {'input_per_million': 5.5, 'output_per_million': 16.5}
PRICING_SOURCE = 'https://developers.openai.com/api/docs/models/gpt-6.1-sol'
AREAS = {
    'kubernetes': {
        'workqueue': ['staging/src/k8s.io/client-go/util/workqueue/'],
        'flowcontrol': ['staging/src/k8s.io/client-go/util/flowcontrol/'],
        'rest': ['staging/src/k8s.io/client-go/rest/'],
        'wait': ['staging/src/k8s.io/apimachinery/pkg/util/wait/'],
    },
    'httpd': {
        'proxy': ['modules/proxy/'],
        'cache': ['modules/cache/'],
        'documentation': ['docs/manual/mod/mod_proxy', 'docs/manual/mod/mod_cache'],
    },
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


def event(directory, kind, **detail):
    item = {'time': time.time(), 'kind': kind, **detail}
    with (Path(directory) / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return item


def area_for(repository, path):
    for area, prefixes in AREAS[repository].items():
        if any(path.startswith(prefix) for prefix in prefixes):
            if area == 'documentation' and not path.endswith('.xml'):
                return None
            return area
    return None


def split_source(data, max_bytes=MAX_SOURCE_BYTES, max_lines=1000):
    """Keep raw UTF-8 source lines intact; offsets live in separate metadata."""
    data.decode('utf-8')
    lines = data.splitlines(keepends=True)
    if any(len(line) > max_bytes for line in lines):
        raise ValueError('A single source line exceeds the source-size bound')
    current, size, start = [], 0, 1
    for number, line in enumerate(lines, 1):
        if current and (size + len(line) > max_bytes or len(current) >= max_lines):
            yield start, number - 1, b''.join(current)
            current, size, start = [], 0, number
        current.append(line)
        size += len(line)
    if current or not lines:
        yield start, len(lines), b''.join(current)


def snapshot_repository(repo, repository, workspace):
    """Inventory every sparse file, export selected tracked UTF-8 files only."""
    if repository not in AREAS:
        raise ValueError('Repository must be kubernetes or httpd')
    if git(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Public source repository has tracked modifications')
    tracked = set(git(repo, 'ls-files', '-z').split('\0'))
    files, chunks = [], []
    for base, directories, names in os.walk(repo, followlinks=False):
        directories[:] = sorted(d for d in directories if d != '.git')
        for name in sorted(names):
            path = Path(base) / name
            relative = path.relative_to(repo).as_posix()
            if path.is_symlink():
                files.append({'path': relative, 'included': False, 'reason': 'symlink'})
                continue
            raw = path.read_bytes()
            area = area_for(repository, relative)
            record = {'path': relative, 'bytes': len(raw), 'sha256': digest(raw), 'included': False}
            if relative not in tracked:
                record['reason'] = 'untracked'
            elif not area:
                record['reason'] = 'outside bounded areas'
            else:
                try:
                    pieces = list(split_source(raw))
                except (UnicodeDecodeError, ValueError) as exc:
                    record['reason'] = str(exc)
                else:
                    record.update(included=True, area=area, chunks=[])
                    for part, (start, end, content) in enumerate(pieces, 1):
                        locator = f'sources/{area}/{relative}.part-{part:04d}'
                        target = workspace / locator
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        chunk = {'locator': locator, 'original_path': relative,
                                 'original_start_line': start, 'original_end_line': end,
                                 'chunk_start_line': 1, 'bytes': len(content), 'sha256': digest(content), 'area': area}
                        chunks.append(chunk)
                        record['chunks'].append(locator)
            files.append(record)
    if not chunks:
        raise ValueError('No tracked UTF-8 source files in configured areas')
    return {'repository': repository, 'origin': git(repo, 'remote', 'get-url', 'origin'),
            'commit': git(repo, 'rev-parse', 'HEAD'), 'source_repository': str(repo),
            'prepared': time.time(), 'inventory': files, 'chunks': chunks}


def create_catalog(workspace, snapshot):
    pages = []
    for area in AREAS[snapshot['repository']]:
        rows = [c for c in snapshot['chunks'] if c['area'] == area]
        for page, offset in enumerate(range(0, len(rows), 25), 1):
            locator = f'catalog/{area}-{page:03d}.jsonl'
            path = workspace / locator
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in rows[offset:offset + 25]), encoding='utf-8')
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError('Catalog page exceeds source-size bound')
            pages.append({'locator': locator, 'area': area, 'chunks': len(rows[offset:offset + 25])})
    index = '\n'.join(f"{p['area']}: {p['locator']} ({p['chunks']} source chunks)" for p in pages) + '\n'
    (workspace / 'CATALOG.md').write_text(index, encoding='utf-8')
    return pages


def prepare(repository, repo, directory, model=DEFAULT_MODEL):
    repo, directory = Path(repo).resolve(), Path(directory).resolve()
    if directory.exists():
        raise ValueError(f'Experiment directory already exists: {directory}; resume or choose a fresh directory')
    directory.mkdir(parents=True)
    workspace = directory / 'workspace'
    workspace.mkdir()
    try:
        snapshot = snapshot_repository(repo, repository, workspace)
        pages = create_catalog(workspace, snapshot)
        write_json(directory / 'source-snapshot.json', snapshot)
        manifest = {key: snapshot[key] for key in ('repository', 'origin', 'commit', 'prepared')}
        manifest.update(inventory_files=len(snapshot['inventory']), selected_files=sum(f['included'] for f in snapshot['inventory']),
                        source_chunks=len(snapshot['chunks']), catalog_pages=pages,
                        mapping='Original line = chunk read line + original_start_line - 1. Source chunks contain unchanged raw UTF-8 lines.',
                        limits='Each source chunk and catalog page is at most 128 KiB. Catalog rows enumerate every exported chunk.')
        write_json(workspace / 'MANIFEST.json', manifest)
        areas = ', '.join(AREAS[repository])
        brief = (
            f'Research-only public-repository experiment: {snapshot["origin"]} at exact commit {snapshot["commit"]}. '
            f'Bounded areas: {areas}. Find which correctness, reliability or maintainability uncertainties are worth '
            'investigating and why; the harness supplies no suspected bug or preferred hypothesis. '
            'Choose discovery scope, rank alternatives, then independently evaluated falsifiable research. '
            'Consider existing tests and contracts, contradictory evidence, false positives, and implementation cost. '
            'A credible no-defect conclusion or falsified hypothesis is valuable. Do not invent production evidence. '
            'No shell, code modifications, test execution, PRs, pushes or deployments are authorized in this experiment. '
            'Only discover, research, retry_research, pause, complete are executable. '
            'All inputs live in an isolated source snapshot, not the public checkout. '
            'List resources to verify filenames. MANIFEST.json and CATALOG.md describe the snapshot; catalog/*.jsonl '
            'lists exact source chunk paths and original file/line mapping. Source chunks preserve raw code. '
            'Read line n in a source chunk as original line n + original_start_line - 1. '
            'Catalog paths: ' + ', '.join(p['locator'] for p in pages) + '. '
            'Directory scopes/globs are invalid: select explicit files. Discovery may read catalog pages first and '
            'identify useful next scopes, but a catalog is not evidence of a code defect. '
            'Use tool/time budgets sufficient to inspect actual source, record checkpoints, and review progress before '
            'more spending. Each plan must state decision, expected_value compared with alternatives, and stop_condition. '
            'Finish with a ranked agenda, evidence-backed conclusions, unresolved gaps and concrete next verification steps. '
            'Maintain coverage of the bounded areas, but prefer important uncertainty to exhaustive reading. '
            'Total API budget is $25, durably enforced across all roles and restarts; the harness may stop earlier.'
        )
        if len(brief) > 20000:
            raise ValueError('Research briefing exceeds context bound')
        (workspace / 'BRIEF.md').write_text(brief + '\n', encoding='utf-8')
        store = Store(directory / 'state.db')
        goal = store.create_goal(
            f'Independently map and prioritize valuable research in {repository} ({areas}), investigate the strongest '
            'falsifiable questions, and retain a reusable evidence trail and honest next-step recommendations. Research only.',
            ['Map important contracts and uncertainties using current source citations.',
             'Compare candidate investigations by expected benefit, confidence, cost and falsification; independently evaluate at least two distinct hypotheses.',
             'Evaluate selected hypotheses against source and existing tests; distinguish static evidence from executed validation.',
             'Retain durable agenda, checkpoints, evidence, rejected hypotheses and unresolved coverage gaps.'], workspace)
        control = Control(store)
        control.initialize(goal)
        proposal = control.propose(goal, 'policy', {'instructions': brief})
        control.accept(goal, proposal)
        proposal = control.propose(goal, 'research_context', {'brief': brief, 'rubric': [
            'Claims cite relevant captured source at the pinned revision; catalog metadata alone cannot establish a behavior claim.',
            'Analyze contradictory evidence and existing tests; label unexecuted validation and uncertain consequences explicitly.',
            'Explain the decision affected and value of this investigation compared with alternatives; report remaining gaps.',
            'Stay within the selected hypothesis and stop condition, with an evidence-backed checkpoint before final report.'
        ]})
        control.accept(goal, proposal)
        Research(store).configure(goal, max_runs=24)
        from omnitrix.budget import Budget
        Budget.configure(store, goal, limit_usd=25, prices={model: DEFAULT_PRICES})
        metadata = {'version': 1, 'repository': repository, 'origin': snapshot['origin'], 'commit': snapshot['commit'],
                    'goal_id': goal, 'model': model, 'directory': str(directory), 'budget_usd': 25,
                    'pricing': {'rates': DEFAULT_PRICES, 'source': PRICING_SOURCE, 'interpretation': 'Conservative estimate, not invoice'},
                    'state': 'prepared', 'prepared': time.time(), 'read_only': True, 'restarts': []}
        write_json(directory / 'experiment.json', metadata)
        control.event(goal, 'public_experiment_prepared', {'repository': repository, 'commit': snapshot['commit'], 'budget_usd': 25})
        store.db.close()
        event(directory, 'prepared', goal_id=goal, commit=snapshot['commit'])
        export(directory)
        return metadata
    except BaseException as exc:
        event(directory, 'prepare_exception', error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
        raise


def upgrade_full_repository(directory, repo=None):
    """Expand the same experiment and immutable budget to all tracked text files.

    Previous source copies, requests and state are retained. The new snapshot uses
    a separate prefix; source exclusions are explicit inventory entries.
    """
    from omnitrix.audit import Audit
    directory = Path(directory).resolve()
    metadata = json.loads((directory / 'experiment.json').read_text())
    previous = json.loads((directory / 'source-snapshot.json').read_text())
    repo = Path(repo or previous['source_repository']).resolve()
    if metadata.get('scope') == 'whole_repository':
        raise ValueError('This experiment already has a whole-repository snapshot; resume it without resetting its budget')
    if git(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Public source repository has tracked modifications')
    if git(repo, 'rev-parse', 'HEAD') != metadata['commit']:
        raise ValueError('Whole-repository upgrade must preserve the original pinned commit')
    workspace = directory / 'workspace'
    history = directory / 'history'
    history.mkdir(exist_ok=True)
    for source, destination in [(directory / 'source-snapshot.json', history / 'bounded-source-snapshot.json'),
                                (workspace / 'MANIFEST.json', history / 'bounded-manifest.json'),
                                (workspace / 'BRIEF.md', history / 'bounded-brief.md')]:
        if not destination.exists():
            destination.write_bytes(source.read_bytes())
    inventory, chunks, groups = [], [], {}
    paths = git(repo, 'ls-files', '-z').split('\0')
    for relative in sorted(p for p in paths if p):
        source = repo / relative
        top = relative.split('/')[0] if '/' in relative else '.'
        tag = '__root__' if top == '.' else top
        group = groups.setdefault(top, {'path': top, 'tag': tag, 'files': 0, 'bytes': 0, 'exported_files': 0,
                                        'source_prefixes': [f'sources/full/{tag}'], 'kind': 'repository area'})
        group['files'] += 1
        record = {'path': relative, 'area': top, 'included': False}
        if source.is_symlink():
            record.update(reason='symlink', target=os.readlink(source))
        elif not source.is_file():
            record.update(reason='missing file or submodule')
        else:
            raw = source.read_bytes()
            group['bytes'] += len(raw)
            record.update(bytes=len(raw), sha256=digest(raw))
            try:
                if b'\x00' in raw:
                    raise ValueError('binary NUL content')
                pieces = list(split_source(raw))
            except (UnicodeDecodeError, ValueError) as exc:
                record['reason'] = str(exc)
            else:
                record.update(included=True, chunks=[])
                group['exported_files'] += 1
                for part, (start, end, content) in enumerate(pieces, 1):
                    locator = f'sources/full/{tag}/{relative}.part-{part:04d}'
                    target = workspace / locator
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    chunk = {'locator': locator, 'original_path': relative, 'original_start_line': start,
                             'original_end_line': end, 'chunk_start_line': 1, 'bytes': len(content),
                             'sha256': digest(content), 'area': top}
                    chunks.append(chunk)
                    record['chunks'].append(locator)
        inventory.append(record)
    if not chunks:
        raise ValueError('Full snapshot produced no text source chunks')
    pages = []
    for area, group in sorted(groups.items()):
        rows = [chunk for chunk in chunks if chunk['area'] == area]
        group['chunks'] = len(rows)
        catalog_dir = f'catalog/full/{group["tag"]}'
        group['catalog_directory'] = catalog_dir
        for page, offset in enumerate(range(0, len(rows), 25), 1):
            locator = f'{catalog_dir}/page-{page:04d}.jsonl'
            target = workspace / locator
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(''.join(json.dumps(chunk, ensure_ascii=False) + '\n' for chunk in rows[offset:offset+25]), encoding='utf-8')
            if target.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError('Full catalog page exceeds research source limit')
            pages.append({'locator': locator, 'area': area, 'chunks': len(rows[offset:offset+25])})
        event(directory, 'full_area_exported', area=area, files=group['files'], chunks=group['chunks'])
    summary_lines = ['Whole repository inventory. Every tracked path is recorded; exported text is unchanged.',
                     'Rows: area | tracked files | exported text files | bytes | catalog directory']
    summary_lines.extend(f'{area} | {group["files"]} | {group["exported_files"]} | {group["bytes"]} | {group["catalog_directory"]}'
                         for area, group in sorted(groups.items()))
    (workspace / 'catalog/full/INDEX.md').write_text('\n'.join(summary_lines) + '\n', encoding='utf-8')
    snapshot = {'repository': metadata['repository'], 'origin': metadata['origin'], 'commit': metadata['commit'],
                'source_repository': str(repo), 'prepared': time.time(), 'scope': 'whole_repository',
                'inventory': inventory, 'chunks': chunks, 'areas': list(groups.values())}
    write_json(directory / 'source-snapshot.json', snapshot)
    manifest = {key: snapshot[key] for key in ('repository', 'origin', 'commit', 'prepared', 'scope')}
    manifest.update(inventory_files=len(inventory), exported_text_files=sum(row['included'] for row in inventory),
                    source_chunks=len(chunks), areas=list(groups.values()), catalog_index='catalog/full/INDEX.md',
                    catalog_pages=len(pages),
                    exclusions=[{'path': row['path'], 'reason': row['reason']} for row in inventory if not row['included']],
                    mapping='Original line = chunk read line + original_start_line - 1. Catalog pages carry each chunk mapping.',
                    legacy='Old bounded inputs remain for provenance; use sources/full and catalog/full for the current whole-repository audit.')
    # MANIFEST can grow beyond one research read due to binary exclusions; split
    # detailed exclusion inventory outside the model source workspace if needed.
    if len((json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8')) > MAX_SOURCE_BYTES:
        exclusions = manifest.pop('exclusions')
        write_json(directory / 'source-exclusions.json', exclusions)
        manifest['excluded_files'] = len(exclusions)
        manifest['exclusions_detail'] = 'Retained in source-exclusions.json beside the database; audit summaries account for all paths.'
    write_json(workspace / 'MANIFEST.json', manifest)
    brief = (
        f'WHOLE-REPOSITORY research audit of {metadata["origin"]} at pinned commit {metadata["commit"]}. '
        f'Inventory: {len(inventory)} tracked paths, {sum(row["included"] for row in inventory)} exported UTF-8 text files, '
        f'{len(chunks)} source chunks and {len(groups)} top-level audit areas. All repository areas are in scope, '
        'including dependencies, tests, build/release automation, documentation and generated material. '
        'The earlier subsystem-only experiment is superseded; its requests, reservations and artifacts remain retained. '
        'This is an open-ended audit: the harness supplies no planted hypothesis or preferred area. '
        'First triage the whole frontier using audit_agenda and update_audit: each registered area needs a reasoned '
        'priority, next question, and honest triaged/deferred/investigating/sampled state. Deferral must state why and '
        'what remains unknown. Use complete only after no areas remain unexamined and at least two distinct hypotheses '
        'have independent evaluation, with ranked alternatives and explicit remaining gaps. A whole-repo inventory '
        'or a sampled area is never proof of a complete semantic audit. Never claim exhaustive correctness. '
        'Use find_resources for global path search and search_sources for broad literal source search, then select '
        'explicit bounded sources for discover/research. Navigation results are leads, not formal evidence. '
        'Root MANIFEST.json and catalog/full/INDEX.md summarize the entire tree. Each area has paginated catalog/full '
        'JSONL mapping pages; source locators are sources/full/<area>/<original_path>.part-NNNN. For root files, area '
        'is __root__. Read line n maps to original line n + original_start_line - 1 from the catalog row. '
        'Every source chunk and catalog page is at most 128 KiB; actual source content is unchanged UTF-8. '
        'Avoid deprecated sources outside sources/full. Excluded binary, symlink, non-UTF8, oversized-line or missing '
        'paths are inventory gaps, not audited text. Look for high-value correctness, reliability, security and '
        'maintainability uncertainties, then compare expected benefit, confidence, cost, and falsification before spending. '
        'Investigate independently selected hypotheses, existing tests, and contradictions; credible falsification and '
        'no-defect outcomes are valuable. Label unexecuted tests and unsupported production impacts. '
        'Record evidence-backed checkpoints during research and reassess drift and portfolio coverage after every result. '
        'Each plan requires decision, expected_value and stop_condition. Do not pause merely because work is long; '
        'continue valuable research within the durable budget, and stop for actual blockers or low expected value. '
        'Research only: no shell, test execution, code modifications, Codex delegation, PRs, pushes, or deployments. '
        'Allowed actions: discover, research, retry_research, schedule_research, reevaluate, pause, complete. Use schedule_research '
        'to persist up to six fully specified plans for fair execution without a new manager call per worker. Every plan '
        'must name a registered primary_area and typed obligations with supporting and contradicting source scopes. Total budget stays $25 including every '
        'prior request and uncertainty reservation; this scope upgrade never refills it. Use long-running bounded '
        'research attempts as useful, but the manager retains durable frontier priorities and unresolved questions.'
    )
    (workspace / 'BRIEF.md').write_text(brief + '\n', encoding='utf-8')
    store = Store(directory / 'state.db')
    try:
        control, goal = Control(store), metadata['goal_id']
        proposal = control.propose(goal, 'policy', {'instructions': brief})
        control.accept(goal, proposal)
        proposal = control.propose(goal, 'requirements', {'constraint':
            'Whole repository research replaces the earlier bounded subsystem sample. Triage every registered area, independently '
            'evaluate at least two distinct hypotheses, and retain ranked alternatives, exclusions and remaining gaps. '
            'Inventory and sampling never establish exhaustive semantic correctness. Budget remains $25 including prior requests.',
            'add_requirements': ['Whole-repository frontier triage: every inventory area has a justified priority and next question; no unexamined areas before completion.',
                                 'Report ranked remaining gaps and explicit text-export exclusions without claiming exhaustive semantic audit coverage.']})
        control.accept(goal, proposal)
        proposal = control.propose(goal, 'research_context', {'brief': brief, 'require_checkpoint': True, 'require_evidence_plan': True, 'rubric': [
            'Claims cite relevant captured whole-repository source at the pinned revision; catalog and navigation metadata cannot establish behavior claims.',
            'Analyze contradictory evidence and existing tests; label unexecuted validation and uncertain consequences explicitly.',
            'Explain the decision affected and value compared with alternative areas; report remaining gaps and exclusions without implying exhaustive coverage.',
            'Stay within the hypothesis and stop condition, with an evidence-backed checkpoint before final report.'
        ]})
        control.accept(goal, proposal)
        Audit(store, goal).initialize([{key: group[key] for key in ('path', 'files', 'bytes', 'kind', 'source_prefixes')} for group in groups.values()])
        store.execute('UPDATE research_limits SET max_runs=100 WHERE goal_id=?', (goal,))
        control.event(goal, 'whole_repository_scope_upgraded', {'areas': len(groups), 'inventory_files': len(inventory),
                      'exported_files': sum(row['included'] for row in inventory), 'source_chunks': len(chunks),
                      'budget_reset': False})
    finally:
        store.db.close()
    metadata.update(scope='whole_repository', state='prepared', upgraded=time.time(), areas=len(groups),
                    tracked_files=len(inventory), exported_text_files=sum(row['included'] for row in inventory))
    write_json(directory / 'experiment.json', metadata)
    event(directory, 'whole_repository_upgraded', inventory_files=len(inventory), source_chunks=len(chunks), budget_reset=False)
    return export(directory)


class DisabledRuntime:
    """No fallback to a shell-capable Codex runtime, even on a bad decision."""
    on_event = None

    async def close(self):
        pass

    def __getattr__(self, name):
        raise RuntimeError(f'External runtime action {name} is disabled in the research-only harness')


class ReadOnlySupervisor(Supervisor):
    async def dispatch(self, decision):
        if decision.action not in ('discover', 'research', 'retry_research', 'reevaluate', 'schedule_research', 'pause', 'complete'):
            raise ValueError(f'{decision.action} is prohibited: this experiment authorizes typed research only')
        if decision.action == 'complete':
            discovery = self.db.execute("SELECT 1 FROM research_jobs WHERE goal_id=? AND stage='discovery' AND status='discovered'", (self.goal_id,)).fetchone()
            research = self.db.execute("SELECT count(DISTINCT json_extract(j.spec,'$.hypothesis')) FROM research_runs r JOIN research_jobs j ON r.job_id=j.id WHERE j.goal_id=? AND j.stage='research' AND r.evaluation IS NOT NULL", (self.goal_id,)).fetchone()[0]
            if not discovery or research < 2:
                raise ValueError('Completion needs observed discovery and at least two distinct independently evaluated focused hypotheses; choose the next bounded investigation')
        if decision.action == 'complete':
            from omnitrix.audit import Audit
            coverage = Audit(self.store, self.goal_id).summary()
            if coverage['by_status']['unexamined']:
                raise ValueError('Whole-repository completion requires reasoned triage or explicit deferral for every registered area; unexamined areas remain')
            if coverage['areas_total']:
                remaining = self.db.execute("""SELECT count(*) FROM audit_areas a JOIN audit_area_updates u ON u.goal_id=a.goal_id AND u.area=a.path
                    AND u.revision=(SELECT max(v.revision) FROM audit_area_updates v WHERE v.goal_id=a.goal_id AND v.area=a.path)
                    WHERE a.goal_id=? AND a.kind!='subarea' AND u.status!='sampled'""",(self.goal_id,)).fetchone()[0]
                if remaining:
                    raise ValueError(f'Whole-repository audit remains incomplete: {remaining} top-level areas lack independent source sampling. Triage and deferral do not complete an audit. Continue useful bounded work or pause with budget/coverage gaps.')
        return await super().dispatch(decision)


def sqlite_tables(connection):
    result = {}
    connection.row_factory = sqlite3.Row
    names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = [dict(row) for row in connection.execute('SELECT * FROM ' + quoted)]
    return result


def backup_database(source, destination):
    source = Path(source)
    if not source.exists():
        return {}
    with closing(sqlite3.connect(source)) as incoming, closing(sqlite3.connect(destination)) as outgoing:
        incoming.backup(outgoing)
        outgoing.execute("PRAGMA journal_mode=DELETE")
        integrity = outgoing.execute('PRAGMA integrity_check').fetchall()
        tables = sqlite_tables(outgoing)
    # Explicit reopen verifies the standalone backup, without relying on WAL files.
    with closing(sqlite3.connect(destination)) as reopened:
        reopen_ok = reopened.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    return {'integrity': [r[0] for r in integrity], 'reopen_ok': reopen_ok,
            'table_counts': {k: len(v) for k, v in tables.items()}, 'tables': tables}


def source_integrity(directory):
    snapshot = json.loads((directory / 'source-snapshot.json').read_text())
    changed = []
    for chunk in snapshot['chunks']:
        target = directory / 'workspace' / chunk['locator']
        if not target.is_file() or digest(target.read_bytes()) != chunk['sha256']:
            changed.append(chunk['locator'])
    original = Path(snapshot['source_repository'])
    original_changes = None
    if original.is_dir():
        original_changes = []
        if git(original, 'rev-parse', 'HEAD') != snapshot['commit']:
            original_changes.append('HEAD')
        for record in snapshot['inventory']:
            if record.get('sha256'):
                path = original / record['path']
                if not path.is_file() or digest(path.read_bytes()) != record['sha256']:
                    original_changes.append(record['path'])
    return {'snapshot_unchanged': not changed, 'changed_chunks': changed,
            'public_checkout_unchanged': not original_changes if original_changes is not None else None,
            'public_checkout_changes': original_changes}


def export(directory):
    directory = Path(directory).resolve()
    metadata = json.loads((directory / 'experiment.json').read_text())
    target = directory / 'export'
    target.mkdir(exist_ok=True)
    database = backup_database(directory / 'state.db', target / 'state.sqlite')
    sessions = backup_database(directory / 'state.db.sessions.db', target / 'sessions.sqlite')
    write_json(target / 'tables.json', database.pop('tables'))
    write_json(target / 'sessions-tables.json', sessions.pop('tables', {}))
    store = Store(directory / 'state.db')
    goal = metadata['goal_id']
    try:
        from omnitrix.budget import Budget
        budget = Budget(store, goal).summary()
        knowledge = Knowledge(store).export(goal)
        write_json(target / 'knowledge.json', knowledge)
        integrity = source_integrity(directory)
        state = store.snapshot(goal)
        write_json(target / 'snapshot.json', state)
        if metadata.get('scope') == 'whole_repository':
            from omnitrix.audit import Audit
            audit = Audit(store, goal)
            gaps = audit.completion_gaps()
            top_level = audit.summary()['top_level_areas']
            recorded = store.db.execute("""SELECT count(*) FROM audit_areas a JOIN audit_area_updates u
                ON u.goal_id=a.goal_id AND u.area=a.path AND u.revision=(SELECT max(v.revision)
                FROM audit_area_updates v WHERE v.goal_id=a.goal_id AND v.area=a.path)
                WHERE a.goal_id=? AND a.kind!='subarea' AND u.status='sampled'""", (goal,)).fetchone()[0]
            from datetime import datetime, timezone
            write_json(directory / 'completion-check.json', {
                'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                'top_level_areas': top_level, 'recorded_sampled_top_level': recorded,
                'currently_eligible_top_level': top_level-len(gaps), 'gaps': gaps,
                'complete_coverage_floor_met': not gaps,
                'note': 'Current sources and exact goal/policy/context revisions are rechecked; historical agenda updates remain intact. This coverage floor does not establish exhaustive semantic correctness.'})
        report = {'experiment': metadata, 'database': database, 'manager_session': sessions,
                  'source_integrity': integrity, 'budget': budget,
                  'counts': {'decisions': len(state['decisions']), 'research_jobs': len(knowledge['jobs']),
                             'research_runs': sum(len(j['runs']) for j in knowledge['jobs']),
                             'knowledge': len(knowledge['records']), 'opportunities': len(knowledge['opportunities']),
                             'checkpoints': store.db.execute("SELECT count(*) FROM activity WHERE goal_id=? AND kind='research_checkpoint'", (goal,)).fetchone()[0]},
                  'goal_status': state['goal']['status'],
                  'limitations': ['Static source research only; no public-repository code or tests were executed.',
                                 'Evaluator agreement does not independently prove a bug or practical impact.',
                                 'API cost uses conservative configured price bounds; final provider invoice may differ.']}
        write_json(target / 'report.json', report)
        artifact_hashes = [{'path': p.relative_to(directory).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p.read_bytes())}
                           for p in sorted(target.iterdir()) if p.is_file() and p.name != 'artifact-manifest.json' and not p.name.endswith(('-wal','-shm','-journal'))]
        write_json(target / 'artifact-manifest.json', artifact_hashes)
        return report
    finally:
        store.db.close()


def durable_counts(directory):
    result = {}
    for name in ('state.db', 'state.db.sessions.db'):
        path = directory / name
        if path.exists():
            with closing(sqlite3.connect(path)) as connection:
                result[name] = {k: len(v) for k, v in sqlite_tables(connection).items()}
    return result


async def run(directory, steps=72, seconds=5400, restart_every=2, *, supervisor_factory=None):
    """Continue saved state; no automatic retries of uncertain/cancelled work."""
    if not 1 <= steps <= 1000 or not 1 <= seconds <= 86400 or restart_every < 0:
        raise ValueError('Steps/time must be positive and bounded; restart interval must be nonnegative')
    directory = Path(directory).resolve()
    metadata = json.loads((directory / 'experiment.json').read_text())
    if metadata['state'] in ('running', 'interrupted'):
        # Supervisor recovery fences uncertain tasks. A crashed harness is never a
        # reason to reissue requests; the regular advance() reconciliation applies.
        event(directory, 'resuming_uncertain_experiment', previous=metadata['state'])
    if metadata['state'] == 'completed':
        saved = Store(directory / 'state.db')
        completed = saved.snapshot(metadata['goal_id'])['goal']['status'] == 'completed'
        saved.db.close()
        if completed:
            return export(directory)
        event(directory,'reopened_after_goal_revision',budget_reset=False)
    metadata.update(state='running', last_started=time.time())
    write_json(directory / 'experiment.json', metadata)
    started, used, supervisor, store = time.monotonic(), 0, None, None
    deadline = started + seconds
    factory = supervisor_factory or ReadOnlySupervisor
    try:
        while used < steps and time.monotonic() < deadline:
            store = Store(directory / 'state.db')
            goal = metadata['goal_id']
            if used == 0:
                store.execute("UPDATE goals SET status='active' WHERE id=? AND status!='completed'", (goal,))
                Control(store).event(goal, 'experiment_explicit_resume', {'previous_state': metadata.get('state'), 'budget_reset': False})
            supervisor = await factory(store, goal, brain=AgentBrain(model=metadata['model'], store=store),
                runtime=DisabledRuntime(), research=Research(store, ResearchAgents(model=metadata['model']), require_checkpoint=metadata.get('scope')=='whole_repository')).open()
            batch_used = 0
            while used < steps and time.monotonic() < deadline:
                if not supervisor.work:
                    if supervisor.status()['goal']['status'] == 'completed':
                        break
                    before = store.db.execute('SELECT count(*) FROM decisions WHERE goal_id=?', (goal,)).fetchone()[0]
                    result = await asyncio.wait_for(supervisor.advance(steps=1), max(.01, deadline - time.monotonic()))
                    after = store.db.execute('SELECT count(*) FROM decisions WHERE goal_id=?', (goal,)).fetchone()[0]
                    used += max(0, after-before)
                    batch_used += 1
                    event(directory, 'manager_step', step=used, decisions_added=after-before, outcome=result)
                    if result.get('blocked') or supervisor.status()['goal']['status'] in ('completed', 'paused'):
                        break
                if supervisor.work:
                    # Workers finish naturally at an idle restart boundary. The
                    # harness never uses a restart as an implicit retry mechanism.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Experiment wall-clock budget exhausted')
                    await asyncio.wait_for(asyncio.gather(*list(supervisor.work.values())), remaining)
                    event(directory, 'worker_batch_finished', step=used)
                    export(directory)
                if restart_every and batch_used >= restart_every:
                    break
            was_terminal = supervisor.status()['goal']['status'] in ('completed', 'paused')
            # Close and reopen a real DB and real manager-session store at idle.
            await supervisor.close()
            supervisor = None
            before_restart = durable_counts(directory)
            Control(store).event(goal, 'experiment_restart_checkpoint', {'decisions_this_batch': batch_used})
            store.db.close()
            store = None
            after_restart = durable_counts(directory)
            unchanged = all(
                after_restart.get(database, {}).get(table) == count
                for database, tables in before_restart.items() for table, count in tables.items()
                if not (database == 'state.db' and table == 'activity'))
            checkpoint = {'time': time.time(), 'before': before_restart, 'after': after_restart,
                          'persisted_counts_unchanged': unchanged, 'idle': True}
            metadata['restarts'].append(checkpoint)
            write_json(directory / 'experiment.json', metadata)
            event(directory, 'restart_checkpoint', **checkpoint)
            export(directory)
            if was_terminal:
                break
        store = Store(directory / 'state.db')
        status = store.snapshot(metadata['goal_id'])['goal']['status']
        store.db.close()
        store = None
        metadata.update(state='completed' if status == 'completed' else 'stopped', last_steps=used, last_wall_seconds=time.monotonic()-started)
    except BaseException as exc:
        metadata.update(state='interrupted' if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else 'failed',
                        last_error=f'{type(exc).__name__}: {exc}', last_steps=used, last_wall_seconds=time.monotonic()-started)
        event(directory, 'run_exception', error=metadata['last_error'], traceback=traceback.format_exc())
    finally:
        if supervisor:
            await supervisor.close()
        if store:
            store.db.close()
        write_json(directory / 'experiment.json', metadata)
    return export(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Snapshot source and configure durable $25 research experiment; no model calls')
    prep.add_argument('repository', choices=AREAS)
    prep.add_argument('--repo', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--model', default=DEFAULT_MODEL, choices=[DEFAULT_MODEL])
    for command in ('run', 'resume'):
        live = sub.add_parser(command, help='Run or resume real research-only manager with durable budget')
        live.add_argument('directory', type=Path)
        live.add_argument('--steps', type=int, default=72)
        live.add_argument('--seconds', type=int, default=5400)
        live.add_argument('--restart-every', type=int, default=2)
    upgrade = sub.add_parser('upgrade-full', help='Expand an existing same-budget experiment to all tracked repository text')
    upgrade.add_argument('directory', type=Path)
    upgrade.add_argument('--repo', type=Path)
    saved = sub.add_parser('export', help='Export all durable tables and standalone SQLite backups without model calls')
    saved.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.repository, args.repo, args.output, args.model)
    elif args.command == 'upgrade-full':
        result = upgrade_full_repository(args.directory, args.repo)
    elif args.command == 'export':
        result = export(args.directory)
    else:
        result = asyncio.run(run(args.directory, args.steps, args.seconds, args.restart_every))
    if 'experiment' in result:
        result = {'directory': result['experiment']['directory'], 'state': result['experiment']['state'],
                  'goal_status': result['goal_status'], 'counts': result['counts'], 'budget': result['budget'],
                  'report': str(Path(result['experiment']['directory']) / 'export/report.json')}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
