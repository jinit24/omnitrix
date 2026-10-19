#!/usr/bin/env python3
"""Build a standalone, evidence-linked report from public research run databases.

No Omnitrix service or model is invoked. SQLite connections are read-only and each
repository is read in one consistent transaction. Run again after the live jobs
finish to replace a provisional report with the latest durable state.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

LOCAL_ZONE = timezone.utc
PROCESS_STATUSES = {'blocked', 'escalated', 'interrupted', 'stale', 'invalidated'}


def parse_json(value, default=None):
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return default


def rows(connection, table):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"')]


def when(value):
    if not value:
        return '—'
    try:
        stamp = float(value)
    except (ValueError, TypeError):
        try:
            stamp = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return str(value)
    return datetime.fromtimestamp(stamp, LOCAL_ZONE).strftime('%d %b %H:%M:%S UTC')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_url(origin, commit, path, lines=()):
    base = origin[:-4] if origin.endswith('.git') else origin
    if base.startswith('git@github.com:'):
        base = 'https://github.com/' + base.split(':', 1)[1]
    result = f'{base}/blob/{commit}/{quote(path, safe="/")}' if path else f'{base}/tree/{commit}'
    if lines:
        low, high = min(lines), max(lines)
        result += f'#L{low}' + (f'-L{high}' if high != low else '')
    return result


def evidence_records(directory, snapshot, source_rows):
    mapping = {chunk['locator']: chunk for chunk in snapshot.get('chunks', [])}
    old = load_json(directory / 'history/bounded-source-snapshot.json', {})
    mapping.update({chunk['locator']: chunk for chunk in old.get('chunks', []) if chunk['locator'] not in mapping})
    evidence, files, metadata_count = {}, defaultdict(set), 0
    hashes = {}
    for source in source_rows:
        entry = dict(source)
        chunk = mapping.get(source['locator'])
        entry['original_path'] = chunk['original_path'] if chunk else None
        entry['chunk_lines'] = [int(line) for line in re.findall(r'(?m)^(\d+):', source.get('excerpt', ''))]
        entry['original_lines'] = sorted({line + chunk['original_start_line'] - 1 for line in entry['chunk_lines']}) if chunk else []
        entry['source_url'] = original_url(snapshot['origin'], snapshot['commit'], chunk['original_path'], entry['original_lines']) if chunk else None
        target = directory / 'workspace' / source['locator']
        if source['locator'] not in hashes:
            hashes[source['locator']] = sha256(target) if target.is_file() else None
        entry['current_copy_matches_capture'] = hashes[source['locator']] == source.get('revision')
        entry['pinned_chunk_matches_capture'] = chunk['sha256'] == source.get('revision') if chunk else None
        if chunk:
            files[chunk['original_path']].update(entry['original_lines'])
        else:
            metadata_count += 1
        evidence[source['id']] = entry
    captured_files = []
    for path, lines in sorted(files.items()):
        captured_files.append({'original_path': path, 'unique_lines_captured': len(lines), 'line_ids': sorted(lines),
                               'source_url': original_url(snapshot['origin'], snapshot['commit'], path, lines)})
    return evidence, captured_files, metadata_count


def budget_summary(configs, requests):
    config = configs[0] if configs else {}
    limit = config.get('limit_nano', 0)
    committed = sum(row.get('charged_nano', 0) for row in requests)
    settled = sum(row.get('charged_nano', 0) for row in requests if row['state'] in ('settled', 'overrun'))
    uncertain = sum(row.get('charged_nano', 0) for row in requests if row['state'] == 'uncertain')
    pending = sum(row.get('charged_nano', 0) for row in requests if row['state'] == 'reserved')
    roles = defaultdict(lambda: {'requests': 0, 'committed_usd': 0, 'input_tokens': 0, 'output_tokens': 0})
    for row in requests:
        role = roles[row['role']]
        role['requests'] += 1
        role['committed_usd'] += row.get('charged_nano', 0) / 1e9
        role['input_tokens'] += row.get('input_tokens') or 0
        role['output_tokens'] += row.get('output_tokens') or 0
    return {'limit_usd': limit / 1e9, 'committed_usd': committed / 1e9,
            'measured_usage_estimate_usd': settled / 1e9, 'uncertain_reservations_usd': uncertain / 1e9,
            'pending_reservations_usd': pending / 1e9, 'remaining_usd': max(0, (limit - committed) / 1e9),
            'requests': len(requests), 'request_states': dict(Counter(row['state'] for row in requests)),
            'roles': dict(roles), 'prices': parse_json(config.get('prices'), {}),
            'cost_basis': 'Conservative configured rates applied to measured provider tokens, plus full reservations for uncertain/pending requests. Not a provider invoice.'}


def http_error_incidents(requests):
    """Attribute causes only when the retained provider diagnostic names them."""
    groups = defaultdict(list)
    for request in requests:
        diagnostic = request.get('error') or ''
        if 'http 400' not in diagnostic.casefold():
            continue
        lower = diagnostic.casefold()
        if 'no tool call found for function call output' in lower:
            cause = 'orphan_function_call_output'
        elif 'reasoning' in lower and 'none' in lower and any(word in lower for word in ('unsupported', 'not supported', 'invalid', 'allowed', 'supported values')):
            cause = 'unsupported_reasoning_effort'
        else:
            cause = 'http400_unclassified'
        groups[cause].append(request)
    descriptions = {
        'orphan_function_call_output': 'HTTP 400 provider diagnostics identify a function-call output without a matching tool call in request history.',
        'unsupported_reasoning_effort': "HTTP 400 provider diagnostics explicitly reject reasoning effort 'none'.",
        'http400_unclassified': 'HTTP 400 request rejection with an unclassified cause: the retained diagnostic does not identify the rejected parameter or history problem.'
    }
    return [{'kind': cause, 'description': descriptions[cause] + ' All affected request reservations remain in the same per-repository budget ledger.',
             'request_ids': [request['id'] for request in group], 'http400_requests': len(group),
             'retained_diagnostics': [{'request_id': request['id'], 'error': request.get('error')} for request in group]}
            for cause, group in groups.items()]


def execution_timing(metadata, requests):
    requests = sorted(requests, key=lambda row: row.get('created', 0))
    if not requests:
        return {}
    first = requests[0]['created']
    latest = max(row.get('finished') or row['created'] for row in requests)
    intervals = sorted((row['created'], row['finished']) for row in requests if row.get('finished'))
    merged = []
    for start, finish in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(finish, merged[-1][1])
        else:
            merged.append([start, finish])
    settled_manager = [row for row in requests if row['role'] == 'manager' and row['state'] == 'settled']
    request_sizes = [{key: row.get(key) for key in ('id', 'created', 'request_bytes', 'input_tokens', 'output_tokens')} for row in settled_manager]
    return {'first_request': first, 'last_recorded_request_activity': latest,
            'elapsed_from_first_request_seconds': latest-first,
            'preparation_to_last_request_seconds': latest-metadata.get('prepared', first),
            'recorded_completed_request_wall_seconds': sum(finish-start for start,finish in merged),
            'timing_note': 'Elapsed period includes gaps, operator work and restarts. Request wall time is the union of recorded completed request intervals; unfinished reservations are excluded.',
            'settled_manager_request_sizes': request_sizes,
            'request_size_note': 'Request bytes and measured input tokens are descriptive observations during workflow changes. They are not a controlled causal comparison or a quality/speed benchmark.'}


def event_description(kind, detail):
    if kind == 'decision':
        return f"{detail.get('action', 'decision')}: {detail.get('rationale', '')}"
    if kind == 'research_checkpoint':
        return f"Remaining uncertainty: {detail.get('uncertainty', '')}; next: {detail.get('next_step', '')}"
    if kind in ('worker_started', 'worker_finished'):
        return str(detail.get('kind') or (detail.get('result') or {}).get('status') or 'research worker')
    if kind == 'live_recovery_verified':
        return f"Reconciled without replay: request count {detail.get('requests_before')} → {detail.get('requests_after')}"
    if kind == 'live_fault_injection':
        return f"Injected {detail.get('kind', 'fault')} during {len(detail.get('jobs', []))} active research job(s)"
    return str(detail.get('message') or detail.get('reason') or detail.get('error') or detail.get('feedback') or '')


def read_repository(directory):
    directory = Path(directory).resolve()
    metadata = load_json(directory / 'experiment.json', {})
    snapshot = load_json(directory / 'source-snapshot.json', {})
    uri = (directory / 'state.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        names = ['goals', 'decisions', 'research_jobs', 'research_runs', 'research_reviews', 'research_sources',
                 'research_queries', 'research_run_contexts', 'research_limits', 'research_context_versions',
                 'knowledge', 'opportunities', 'audit_areas', 'audit_area_updates', 'activity', 'messages',
                 'task_runtime', 'dollar_budgets', 'dollar_requests', 'verifications', 'implementations']
        tables = {name: rows(connection, name) for name in names}
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        connection.rollback()
    jobs = {job['id']: dict(job, spec=parse_json(job['spec'], {})) for job in tables['research_jobs']}
    evidence, captured_files, metadata_evidence_count = evidence_records(directory, snapshot, tables['research_sources'])
    assessments = []
    for run in tables['research_runs']:
        job = jobs.get(run['job_id'], {})
        assessments.append({**run, 'stage': job.get('stage'), 'spec': job.get('spec', {}),
                            'report': parse_json(run.get('report')), 'evaluation': parse_json(run.get('evaluation')),
                            'kind': 'original_run', 'current_job_status': job.get('status')})
    for review in tables['research_reviews']:
        job = jobs.get(review['job_id'], {})
        original = next((run for run in assessments if run.get('id') == review.get('run_id')), {})
        context = parse_json(review.get('context'), {})
        assessments.append({**review, 'stage': job.get('stage'), 'spec': job.get('spec', {}),
                            'report': original.get('report') or context.get('report'),
                            'evaluation': parse_json(review.get('evaluation')), 'kind': 'additional_review',
                            'current_job_status': job.get('status')})
    assessments.sort(key=lambda row: row.get('created', 0))
    normalized = lambda value: ' '.join(value.casefold().split())
    evaluated = {normalized(row['spec']['hypothesis']) for row in assessments if row['spec'].get('hypothesis') and row.get('evaluation')}
    latest = {}
    for row in assessments:
        hypothesis = row['spec'].get('hypothesis')
        if hypothesis and row.get('evaluation'):
            latest[normalized(hypothesis)] = row
    latest_updates = {}
    for update in tables['audit_area_updates']:
        if update['area'] not in latest_updates or update['revision'] > latest_updates[update['area']]['revision']:
            latest_updates[update['area']] = update
    areas = []
    top_level = {area['path'] for area in snapshot.get('areas', [])}
    for area in tables['audit_areas']:
        update = latest_updates.get(area['path'], {})
        areas.append({**area, 'source_prefixes': parse_json(area.get('source_prefixes'), []),
                      'status': update.get('status', 'unexamined'), 'priority': update.get('priority'),
                      'rationale': update.get('rationale', ''), 'next_question': update.get('next_question', ''),
                      'revision': update.get('revision'), 'job_id': update.get('job_id'),
                      'top_level_inventory_area': area['path'] in top_level})
    areas.sort(key=lambda area: (area.get('priority') or 99, area['path']))
    activities = [{**row, 'detail': parse_json(row['detail'], {})} for row in tables['activity']]
    interventions = [row for row in activities if row['kind'].startswith('operator_') or row['kind'] in ('structured_contract_diagnosed', 'live_fault_injection', 'whole_audit_completion_corrected', 'workflow_reload_requested')]
    timeline_kinds = {'decision', 'research_checkpoint', 'worker_started', 'worker_finished', 'recovery',
                      'experiment_restart_checkpoint', 'live_fault_injection', 'live_recovery_verified',
                      'manager_budget_exhausted', 'manager_output_invalid', 'structured_contract_diagnosed',
                      'research_report_saved', 'research_evaluation_review', 'whole_audit_completion_corrected', 'workflow_reload_requested'}
    timeline = [{'time': row['created'], 'kind': row['kind'], 'detail': event_description(row['kind'], row['detail']),
                 'activity_id': row['id']} for row in activities if row['kind'] in timeline_kinds or row['kind'].startswith('operator_')]
    for request in tables['dollar_requests']:
        if request.get('error'):
            timeline.append({'time': request.get('finished') or request['created'], 'kind': 'request_' + request['state'],
                             'detail': f"{request['role']}: {request['error']}", 'request_id': request['id']})
    timeline.sort(key=lambda row: row['time'])
    control_probe = load_json(directory / 'control-probe.json', {})
    recovery = load_json(directory / 'live-recovery.json', {})
    completion_check = load_json(directory / 'completion-check.json')
    incidents = http_error_incidents(tables['dollar_requests'])
    for row in activities:
        if row['kind'] == 'structured_contract_diagnosed':
            incidents.append({'kind': 'model_schema_contract', 'description': event_description(row['kind'], row['detail']), 'activity_id': row['id']})
        if row['kind'] == 'whole_audit_completion_corrected':
            incidents.append({'kind':'premature_completion_gate', 'description':'The earlier trial could complete after triage and two hypotheses. The goal was reopened without resetting its budget; top-level areas now require independently evaluated source samples before completion. Deferred and triaged areas remain incomplete.', 'activity_id':row['id']})
    inventory = snapshot.get('inventory', [])
    exclusions = [row for row in inventory if not row.get('included')]
    context_packets = []
    for packet in tables['research_run_contexts']:
        context = parse_json(packet.get('context'), {})
        revisions = parse_json(packet.get('revisions'), {})
        context_packets.append({'run_id': packet['run_id'], 'role': packet['role'], 'created': packet.get('created'),
            'research_context_version': context.get('research_context', {}).get('version'),
            'rubric_items': len(context.get('research_context', {}).get('rubric', [])),
            'goal_revision': revisions.get('goal', {}).get('revision'),
            'process_attestation': context.get('process_attestation'),
            'researcher_checkpoint_count': len(context.get('researcher_checkpoints', []))})
    sessions = {}
    sessions_path = directory / 'state.db.sessions.db'
    if sessions_path.exists():
        with closing(sqlite3.connect(sessions_path.as_uri() + '?mode=ro', uri=True)) as connection:
            for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                sessions[name] = connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
    findings = []
    for assessment in assessments:
        for finding in (assessment.get('report') or {}).get('findings', []):
            verdict = assessment.get('evaluation') or {}
            findings.append({**finding, 'assessment_id': assessment['id'], 'job_id': assessment['job_id'],
                             'evaluation_verdict': verdict.get('verdict'),
                             'evidential_verdict': verdict.get('evidential_verdict'),
                             'workflow_issues': verdict.get('workflow_issues', [])})
    return {'repository': metadata.get('repository', directory.name),
            'name': 'Apache HTTP Server' if metadata.get('repository') == 'httpd' else 'Kubernetes',
            'origin': snapshot.get('origin', metadata.get('origin')), 'commit': snapshot.get('commit', metadata.get('commit')),
            'goal_id': metadata.get('goal_id'), 'model': metadata.get('model'), 'scope': snapshot.get('scope'),
            'experiment_state': metadata.get('state'), 'goal_status': tables['goals'][0]['status'] if tables['goals'] else None,
            'database_integrity': integrity, 'read_at_utc': datetime.now(timezone.utc).isoformat(),
            'budget': budget_summary(tables['dollar_budgets'], tables['dollar_requests']),
            'execution_timing': execution_timing(metadata, tables['dollar_requests']),
            'counts': {'inventory_paths': len(inventory), 'exported_text_files': sum(bool(row.get('included')) for row in inventory),
                'excluded_paths': len(exclusions), 'exported_chunks': len(snapshot.get('chunks', [])),
                'original_files_captured': len(captured_files), 'unique_original_lines_captured': sum(row['unique_lines_captured'] for row in captured_files),
                'captured_evidence_records': len(evidence), 'metadata_evidence_records': metadata_evidence_count,
                'decisions': len(tables['decisions']), 'research_jobs': len(jobs), 'original_research_runs': len(tables['research_runs']),
                'additional_reviews': len(tables['research_reviews']), 'distinct_hypotheses_evaluated': len(evaluated),
                'independent_evaluations': sum(bool(row.get('evaluation')) for row in assessments),
                'latest_overall_verdicts': dict(Counter(row['evaluation'].get('verdict') for row in latest.values())),
                'latest_evidential_verdicts': dict(Counter(row['evaluation'].get('evidential_verdict') or 'not_separated' for row in latest.values())),
                'research_run_statuses': dict(Counter(row['status'] for row in tables['research_runs'])),
                'knowledge_records': len(tables['knowledge']), 'opportunities': len(tables['opportunities']),
                'research_checkpoints': sum(row['kind'] == 'research_checkpoint' for row in activities),
                'persisted_context_packets': len(context_packets), 'control_messages': sum(row['kind'] == 'control' for row in tables['messages']),
                'acknowledged_control_messages': sum(row['kind'] == 'control' and row['state'] == 'acknowledged' for row in tables['messages']),
                'verification_runs': len(tables['verifications']), 'implementations': len(tables['implementations'])},
            'audit_coverage': {'top_level_inventory_areas': len(top_level), 'registered_areas_including_children': len(areas),
                'by_status': dict(Counter(area['status'] for area in areas)), 'areas': areas,
                'note': 'Agenda statuses record historical decisions. Recorded samples require current revision/source revalidation for completion eligibility; inventory, triage and bounded sampling do not establish exhaustive semantic code coverage.'},
            'captured_files': captured_files, 'evidence': evidence, 'assessments': assessments,
            'latest_hypothesis_assessments': list(latest.values()), 'findings': findings,
            'opportunities': [{**row, 'spec': parse_json(row['spec'], {})} for row in tables['opportunities']],
            'context_packets': context_packets, 'context_versions': len(tables['research_context_versions']),
            'requests': tables['dollar_requests'], 'sessions': sessions, 'timeline': timeline,
            'operator_interventions': interventions, 'incidents': incidents, 'control_probe': control_probe,
            'live_recovery': recovery, 'completion_check': completion_check, 'normal_restart_checkpoints': metadata.get('restarts', []),
            'exclusions_by_reason': dict(Counter(row.get('reason', 'unspecified') for row in exclusions)),
            'exclusions': [{'path': row['path'], 'reason': row.get('reason')} for row in exclusions],
            'source_integrity': load_json(directory / 'export/report.json', {}).get('source_integrity'),
            'source_integrity_note': 'Export-level checkout integrity comes from the latest saved export; individual captured-copy hashes above are checked fresh by this generator.',
            'limitations': ['All tracked repository paths were inventoried; only the explicitly counted source excerpts were captured.',
                'Static source inspection and model evaluation do not establish runtime-validated bugs, exploitability, production impact, or a fix.',
                'Distinct hypotheses count normalized text; this does not prove statistical or semantic independence.',
                'Additional reviews are retained beside original evaluations and do not overwrite their history.',
                'The experiment includes operator recovery interventions and workflow changes during execution; it is not an unattended benchmark.',
                'No comparative baseline establishes that this workflow outperforms another agent.',
                'Independent evaluation uses a separate agent context; it is not an independent model family or empirical runtime validation.']}


def esc(value):
    return html.escape(str(value if value is not None else '—'), quote=True)


def badge(value):
    return '<span class="badge">' + esc(value or 'no verdict') + '</span>'


def table(headers, data, classes=''):
    return '<div class="table-scroll"><table class="' + classes + '"><thead><tr>' + ''.join('<th>' + esc(v) + '</th>' for v in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + str(v) + '</td>' for v in row) + '</tr>' for row in data) + '</tbody></table></div>'


def citations(ids, evidence):
    links = []
    for source_id in dict.fromkeys(ids or []):
        source = evidence.get(source_id)
        label = source_id[:8]
        if source and source.get('source_url'):
            lines = source['original_lines']
            label = f'{source["original_path"]}:{min(lines)}–{max(lines)}' if lines else source['original_path']
            links.append(f'<a href="{esc(source["source_url"])}" title="Evidence ID {esc(source_id)}">{esc(label)}</a> <code>{esc(source_id[:8])}</code>')
        elif source:
            links.append(f'<span title="Metadata evidence {esc(source_id)}">{esc(source["locator"])}</span> <code>{esc(source_id[:8])}</code>')
        else:
            links.append('<span class="warning">Unknown evidence ' + esc(source_id) + '</span>')
    return '<br>'.join(links) or 'No citations'


def money(value):
    return f'${value:.3f}'


def workload_svg(repository):
    tasks = repository['assessments']
    if not tasks:
        return '<p>No research attempts yet.</p>'
    low = min(row['created'] for row in tasks)
    high = max(row.get('finished') or datetime.now(timezone.utc).timestamp() for row in tasks)
    span = max(1, high-low)
    width, left, right, row_height = 1000, 210, 30, 36
    inner = width-left-right
    elements = [f'<svg class="timeline-svg" viewBox="0 0 {width} {55+row_height*len(tasks)}" role="img" aria-label="Research workload timeline">']
    elements.append(f'<text x="{left}" y="18">{esc(when(low))}</text><text x="{width-right}" y="18" text-anchor="end">{esc(when(high))}</text>')
    for i, task in enumerate(tasks):
        y = 30 + i*row_height
        start = left + inner*(task['created']-low)/span
        end = left + inner*((task.get('finished') or high)-low)/span
        label = ('Review' if task['kind']=='additional_review' else task['stage'] or 'research') + ' · ' + task['id'][:8]
        color = '#0e7c86' if task['status'] in ('supported','falsified','discovered') else '#d2a35c' if task['status']=='running' else '#8b91a0'
        elements.append(f'<text x="0" y="{y+17}">{esc(label)}</text><rect x="{start:.2f}" y="{y}" width="{max(3,end-start):.2f}" height="23" rx="4" fill="{color}"><title>{esc(label)}: {esc(task["status"])}; {esc(when(task["created"]))} to {esc(when(task.get("finished")))}</title></rect>')
    return ''.join(elements) + '</svg>'


def render_assessment(assessment, evidence):
    spec, evaluation = assessment['spec'], assessment.get('evaluation') or {}
    report = assessment.get('report') or {}
    title = spec.get('hypothesis') or spec.get('question') or assessment['id']
    result = '<details class="assessment"><summary>' + badge(assessment['status']) + ' ' + esc(title) + '</summary>'
    result += '<p class="muted">' + esc(assessment['kind']) + ' · <code>' + esc(assessment['id']) + '</code> · ' + esc(when(assessment['created'])) + ' · job currently ' + esc(assessment.get('current_job_status')) + '</p>'
    result += table(['Overall workflow verdict', 'Evidential verdict', 'Attempt state'], [[badge(evaluation.get('verdict')), badge(evaluation.get('evidential_verdict') or ('not separately recorded' if evaluation else None)), badge(assessment['status'])]])
    if evaluation:
        result += '<p><strong>Evaluator:</strong> ' + esc(evaluation.get('reason', '')) + '</p>'
        if evaluation.get('workflow_issues'):
            result += '<p><strong>Process / gate issues:</strong> ' + esc('; '.join(evaluation['workflow_issues'])) + '</p>'
        result += '<p><strong>Evaluator next step:</strong> ' + esc(evaluation.get('feedback', '')) + '</p>'
    elif assessment['stage'] == 'discovery':
        result += '<p>Discovery is reconnaissance. It has no independent behavioral verdict.</p>'
    else:
        result += '<p>No completed independent evaluation is retained for this attempt.</p>'
    if assessment.get('feedback') or assessment.get('error'):
        result += '<p><strong>Attempt feedback:</strong> ' + esc(assessment.get('error') or assessment['feedback']) + '</p>'
    if report.get('findings'):
        result += table(['Researcher statement (unexecuted)', 'Classification', 'Pinned source / evidence'],
            [[esc(finding['statement']), badge(finding.get('classification')), citations(finding.get('evidence_ids'), evidence)] for finding in report['findings']])
    if report.get('recommendation'):
        result += '<p><strong>Researcher recommendation:</strong> ' + esc(report['recommendation']) + '</p>'
    gaps = report.get('uncertainties') or report.get('gaps') or []
    if gaps:
        result += '<p><strong>Unresolved:</strong></p><ul>' + ''.join('<li>' + esc(gap) + '</li>' for gap in gaps) + '</ul>'
    if evaluation.get('rubric_assessments'):
        result += '<details><summary>Exact custom rubric assessments</summary>' + table(['Rubric criterion', 'Verdict', 'Reason'], [[esc(row['criterion']), badge(row['verdict']), esc(row['reason'])] for row in evaluation['rubric_assessments']]) + '</details>'
    result += '<details><summary>Decision, expected value, stop condition and scope</summary>' + table(['Contract', 'Value'], [[esc(key), esc(spec.get(key, ''))] for key in ('decision','expected_value','stop_condition','falsification')]) + '<ul>' + ''.join('<li><code>' + esc(path) + '</code></li>' for path in spec.get('scope', [])) + '</ul></details>'
    return result + '</details>'


def render_repository(repository):
    r, count, budget = repository, repository['counts'], repository['budget']
    repo_id = r['repository']
    result = f'<section id="{esc(repo_id)}"><div class="section-heading"><p class="eyebrow">Pinned repository experiment</p><h2>{esc(r["name"])}</h2></div>'
    result += '<p><a href="' + esc(original_url(r['origin'], r['commit'], '')) + '">' + esc(r['origin']) + '</a><br><code class="sha">' + esc(r['commit']) + '</code></p>'
    result += '<p class="muted">Goal <code>' + esc(r['goal_id']) + '</code> · model ' + esc(r['model']) + ' · experiment ' + esc(r['experiment_state']) + ' · budgeted research deliverable status ' + esc(r['goal_status']) + '</p>'
    result += table(['Whole-repository inventory', 'Actual captured research evidence'], [
        [f"{count['inventory_paths']:,} tracked paths", f"{count['original_files_captured']:,} unique original repository files read"],
        [f"{count['exported_text_files']:,} UTF-8 text files exported", f"{count['unique_original_lines_captured']:,} distinct original line IDs captured"],
        [f"{count['excluded_paths']:,} explicit export exclusions", f"{count['metadata_evidence_records']:,} separate catalog/brief metadata excerpts"],
        [f"{r['audit_coverage']['top_level_inventory_areas']} top-level areas", f"{count['distinct_hypotheses_evaluated']} distinct normalized hypotheses evaluated"]])
    result += '<p class="notice">The inventory records every tracked path, including explicit text-export exclusions. The captured-file and line counts describe actual observation. Neither establishes exhaustive semantic coverage.</p>'
    result += '<h3>Budget and execution</h3>'
    result += table(['Budget cap', 'Measured-token estimate', 'Uncertain reservations', 'Pending reservations', 'Total committed', 'Remaining'], [[money(budget[key]) for key in ('limit_usd','measured_usage_estimate_usd','uncertain_reservations_usd','pending_reservations_usd','committed_usd','remaining_usd')]])
    result += '<p class="muted">' + esc(budget['cost_basis']) + '</p>'
    result += table(['Role', 'API requests', 'Committed estimate', 'Measured input / output tokens'], [[esc(role), str(values['requests']), money(values['committed_usd']), f"{values['input_tokens']:,} / {values['output_tokens']:,}"] for role, values in sorted(budget['roles'].items())])
    result += '<p>' + esc(count['decisions']) + ' recorded decisions · ' + esc(count['original_research_runs']) + ' original research attempts · ' + esc(count['additional_reviews']) + ' append-only additional reviews · ' + esc(count['research_checkpoints']) + ' saved checkpoints · ' + esc(count['persisted_context_packets']) + ' saved role-context packets.</p>'
    timing = r['execution_timing']
    if timing:
        result += '<p>' + f"{timing['elapsed_from_first_request_seconds']/60:.1f} min from first request to latest recorded request activity · {timing['recorded_completed_request_wall_seconds']/60:.1f} min of recorded completed API request intervals." + '</p><p class="muted">' + esc(timing['timing_note']) + '</p>'
        sizes = timing['settled_manager_request_sizes']
        if sizes:
            selected = sizes[:2] + ([{'separator': True}] if len(sizes)>4 else []) + sizes[max(2,len(sizes)-2):]
            result += '<details><summary>Measured manager request sizes during workflow changes</summary><p>' + esc(timing['request_size_note']) + '</p>' + table(['Request / time', 'Serialized request bytes', 'Measured input tokens'], [['…', '…', '…'] if row.get('separator') else [esc(row['id'][:8]) + '<br>' + esc(when(row['created'])), esc(row.get('request_bytes')), esc(row.get('input_tokens'))] for row in selected]) + '</details>'
    result += '<h3>Hypotheses, findings and independent reviews</h3><p>Source-supported observations and workflow acceptance are shown separately. Statements below are the retained researcher reports; the displayed evaluator disposition controls how much confidence to place in them.</p>'
    if r['latest_hypothesis_assessments']:
        result += table(['Latest assessment per hypothesis', 'Overall workflow / evidence', 'Evaluator conclusion'], [[esc(row['spec']['hypothesis']), badge(row['evaluation'].get('verdict')) + '<br>' + badge(row['evaluation'].get('evidential_verdict') or 'not separately recorded'), esc(row['evaluation'].get('reason')) + '<br><small>' + esc('Additional review' if row['kind']=='additional_review' else 'Original evaluation') + '</small>'] for row in r['latest_hypothesis_assessments']])
    result += ''.join(render_assessment(row, r['evidence']) for row in r['assessments']) or '<p>No research reports yet.</p>'
    result += '<h3>Research agenda and remaining coverage</h3>'
    completion = r.get('completion_check')
    if completion:
        result += '<p>Current completion eligibility checked ' + esc(when(completion.get('checked_at_utc'))) + '. Recorded sample history is distinct from currently eligible evidence after source and revision revalidation.</p>'
        result += table(['Top-level areas', 'Recorded sampled history', 'Currently eligible samples', 'Coverage floor met'], [[esc(completion.get('top_level_areas')), esc(completion.get('recorded_sampled_top_level')), esc(completion.get('currently_eligible_top_level')), 'met' if completion.get('complete_coverage_floor_met') else 'not met']])
        result += '<p class="muted">This is a minimum research completion check, not certification of exhaustive semantic coverage.</p>'
        if completion.get('gaps'):
            result += '<details><summary>Current completion gap reasons</summary>' + table(['Area', 'Recorded state', 'Current eligibility gap'], [[esc(gap.get('path')), badge(gap.get('status')), esc(gap.get('reason'))] for gap in completion['gaps']]) + '</details>'
    result += '<p>Recorded agenda states — ' + esc(', '.join(f'{status}: {number}' for status, number in sorted(r['audit_coverage']['by_status'].items()))) + '. These are historical agenda decisions, not a current eligibility count. Child areas refine the frontier and are not added again to the inventory file count.</p>'
    result += '<details><summary>Recorded priorities, gaps and next questions</summary>' + table(['Area', 'Recorded state / priority', 'Rationale', 'Next unresolved question'], [[esc(area['path']) + (' <small>(child area)</small>' if not area['top_level_inventory_area'] else ''), badge(area['status']) + ' P' + esc(area['priority']), esc(area['rationale']), esc(area['next_question'])] for area in r['audit_coverage']['areas']]) + '</details>'
    if r['opportunities']:
        result += '<details><summary>Retained ranked opportunities</summary>' + table(['Opportunity', 'State / score', 'Reason / scope gaps'], [[esc(row['title']), badge(row['status']) + ' ' + esc(round(row['score'],2)), esc(row['spec'].get('rationale','')) + '<br>' + esc('; '.join(row['spec'].get('scope_gaps',[])))] for row in sorted(r['opportunities'],key=lambda value:-value['score'])]) + '</details>'
    result += '<h3>Durability and operator interventions</h3>'
    probe = r['control_probe']
    result += table(['Check', 'Observed evidence'], [
        ['SQLite integrity', esc(r['database_integrity'])],
        ['Normal idle restart checkpoints', str(len(r['normal_restart_checkpoints']))],
        ['Manager conversation rows', esc(', '.join(f'{name}: {number}' for name, number in r['sessions'].items()) or 'No session database')],
        ['Durable status command', esc(f"{count['acknowledged_control_messages']}/{count['control_messages']} acknowledged; delivery {probe.get('delivery_seconds', 0):.3f}s; reply persisted: {probe.get('reply_persisted', False)}")],
        ['Live process interruption', esc('SIGKILL + recorded recovery' if r['live_recovery'] else 'No live SIGKILL recovery recorded for this repository')],
        ['Executed verifications / implementations', f"{count['verification_runs']} / {count['implementations']}"]])
    if r['live_recovery']:
        recovery = r['live_recovery']
        result += '<p>After injected SIGKILL, reconciliation retained source evidence with request count ' + esc(recovery.get('requests_before')) + ' → ' + esc(recovery.get('requests_after')) + '. This recovery performed no new model calls; future research remained subject to the original budgets.</p>'
    if r['incidents']:
        result += '<p><strong>Observed integration failures:</strong></p><ul>' + ''.join('<li>' + esc(incident['description']) + '</li>' for incident in r['incidents']) + '</ul>'
    if r['operator_interventions']:
        result += '<details><summary>Recorded operator guidance and fault injection</summary>' + table(['Time', 'Intervention', 'Recorded guidance'], [[esc(when(row['created'])), esc(row['kind']), esc(event_description(row['kind'], row['detail']))] for row in r['operator_interventions']]) + '</details>'
    result += '<h3>Workload timeline</h3>' + workload_svg(r)
    result += '<details><summary>Decision, checkpoint, interruption and failure timeline</summary>' + table(['Time (UTC)', 'Event', 'Detail'], [[esc(when(row['time'])), esc(row['kind']), esc(row['detail'])] for row in r['timeline']]) + '</details>'
    result += '<details><summary>Actual captured source files and original line IDs</summary>' + table(['Original repository file', 'Distinct captured lines', 'Line IDs'], [[f'<a href="{esc(row["source_url"])}">{esc(row["original_path"])}</a>', str(row['unique_lines_captured']), '<details><summary>Show line IDs</summary><code>' + esc(', '.join(map(str,row['line_ids']))) + '</code></details>'] for row in r['captured_files']]) + '</details>'
    result += '<details><summary>Export exclusions</summary><p>' + esc(', '.join(f'{reason}: {number}' for reason, number in sorted(r['exclusions_by_reason'].items()))) + '</p>' + table(['Original path', 'Exclusion reason'], [[esc(row['path']), esc(row['reason'])] for row in r['exclusions']]) + '</details>'
    result += '<details><summary>Captured excerpt provenance and hashes</summary>' + ''.join('<details><summary><code>' + esc(source_id) + '</code> ' + esc(source.get('original_path') or source['locator']) + '</summary><p>Current copy matches captured hash: ' + esc(source['current_copy_matches_capture']) + '</p><p>' + citations([source_id],r['evidence']) + '</p><pre>' + esc(source.get('excerpt','')) + '</pre></details>' for source_id, source in r['evidence'].items()) + '</details>'
    return result + '</section>'


def render_html(summary):
    cards = []
    for repository in summary['repositories']:
        count, budget = repository['counts'], repository['budget']
        cards.append('<a class="card" href="#' + esc(repository['repository']) + '"><strong>' + esc(repository['name']) + '</strong><span>' + f"{count['inventory_paths']:,} paths inventoried · {count['original_files_captured']} files captured" + '</span><b>' + money(budget['committed_usd']) + ' / $25 committed</b><small>' + str(count['distinct_hypotheses_evaluated']) + ' distinct hypotheses evaluated</small></a>')
    styles = '''
:root{color-scheme:light;--ink:#172b38;--muted:#576b77;--paper:#f5f7f8;--teal:#096e78;--line:#dce3e7}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,sans-serif}header,main,footer{max-width:1220px;margin:auto;padding:30px 28px}header{padding-top:54px}h1{font-size:42px;line-height:1.1;letter-spacing:-1.3px;margin:12px 0}h2{font-size:30px;margin:5px 0 12px}h3{font-size:20px;margin:30px 0 12px}p{max-width:100ch}a{color:var(--teal);overflow-wrap:anywhere}code{font:12px/1.5 ui-monospace,monospace;overflow-wrap:anywhere}pre{padding:15px;max-height:420px;overflow:auto;background:#edf2f4;font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap}.muted,small{color:var(--muted)}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:1.8px;color:var(--teal);font-weight:700}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:28px 0}.card{display:grid;gap:8px;background:white;border:1px solid var(--line);border-top:4px solid var(--teal);padding:20px;text-decoration:none;color:var(--ink);border-radius:9px}.card strong{font-size:22px}.card b{font-size:24px}.card span{font-size:14px}.flow{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:26px 0}.flow span{background:#e5eff0;padding:8px 12px;border-radius:5px;font-size:13px}.flow i{font-style:normal;color:var(--muted)}section{background:white;padding:28px;border:1px solid var(--line);border-radius:10px;margin:0 0 30px;scroll-margin-top:20px}.notice{background:#eaf3f3;border-left:4px solid var(--teal);padding:12px 16px;font-size:14px}.warning{color:#925612}.badge{display:inline-block;white-space:nowrap;border:1px solid var(--line);background:#eef3f5;padding:2px 7px;border-radius:5px;font-size:12px;margin-right:5px}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 18px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}th{background:#edf3f5;font-size:12px}.table-scroll{overflow-x:auto}details{margin:12px 0;border:1px solid var(--line);padding:12px 14px;border-radius:6px}summary{cursor:pointer;font-weight:600}.assessment>summary{line-height:1.6}.assessment[open]{border-left:4px solid var(--teal)}details details{background:#f9fbfc}.timeline-svg{width:100%;min-width:560px;font:11px system-ui;fill:var(--muted)}.sha{font-size:13px}footer{font-size:13px;color:var(--muted);padding-bottom:45px}@media(max-width:700px){header,main,footer{padding:24px 14px}h1{font-size:34px}section{padding:18px}.timeline-svg{min-width:0}th,td{padding:8px}h2{font-size:27px}}@media print{details{break-inside:avoid}body{background:white}section{border:0;padding:0}header,main{max-width:none}a{color:inherit}}
'''
    lead = 'Whole repositories were inventoried; supported UTF-8 source exports were made searchable, with exclusions retained. The manager prioritized selected questions, retained source evidence, and ran independent evaluation under a persistent $25 cap per repository.'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Omnitrix public repository research report</title><style>' + styles + '</style></head><body><header><p class="eyebrow">Omnitrix · live public repository research</p><h1>What the manager researched,<br>what the evidence supports.</h1><p>' + lead + '</p><p class="muted">Fresh read: ' + esc(when(summary['generated_at'])) + '. Scope, captured coverage, costs, interruptions and operator guidance are retained explicitly.</p><div class="cards">' + ''.join(cards) + '</div><div class="flow" aria-label="Research workflow"><span>Whole-repo inventory</span><i>→</i><span>Durable priorities</span><i>→</i><span>Research + checkpoint</span><i>→</i><span>Independent evaluation</span><i>→</i><span>Retained findings + gaps</span></div><p class="notice">This is static source research with operator interventions. It establishes neither exhaustive semantic audit coverage nor runtime-validated bugs or superiority over another workflow. No PRs were raised.</p></header><main>' + ''.join(render_repository(repository) for repository in summary['repositories']) + '</main><footer>The report combines durable SQLite state read through read-only connections with pinned source snapshots and saved control/recovery artifacts. Full machine-readable details are in <a href="summary.json">summary.json</a>. Public source links point to the exact recorded commits. Cost figures are conservative configured estimates and reservations, not an invoice. Times use UTC.</footer></body></html>'


def generate(run_directories, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary = {'format_version': 1, 'generated_at': datetime.now(timezone.utc).timestamp(),
               'repositories': [read_repository(directory) for directory in run_directories],
               'method': 'Fresh read-only SQLite snapshots per repository; original line mapping from pinned source snapshots. No model calls.',
               'claims': {'whole_repository_inventory': True, 'exhaustive_semantic_audit': False,
                          'runtime_validated_bugs': False, 'comparative_superiority_established': False, 'pull_requests_raised': False}}
    for name, content in [('summary.json', json.dumps(summary, indent=2, ensure_ascii=False) + '\n'),
                          ('audit-report.html', render_html(summary))]:
        target = output / name
        temporary = target.with_name(name + '.tmp')
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(target)
    return {'output': str(output), 'report': str(output / 'audit-report.html'), 'summary': str(output / 'summary.json'),
            'repositories': [{'repository': repository['repository'], 'counts': repository['counts'], 'budget': repository['budget']} for repository in summary['repositories']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, nargs='+', default=[Path('work/public-runs/kubernetes'), Path('work/public-runs/httpd')])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.runs, args.output), indent=2))


if __name__ == '__main__':
    main()
