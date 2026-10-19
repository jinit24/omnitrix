"""Durable repository audit agenda; samples never imply complete coverage."""
import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from .knowledge import Knowledge


STATUSES = ('unexamined', 'triaged', 'investigating', 'sampled', 'deferred')
PAGE_SIZE = 20
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS audit_areas (
      goal_id TEXT NOT NULL REFERENCES goals(id), path TEXT NOT NULL,
      files INTEGER NOT NULL, bytes INTEGER NOT NULL, kind TEXT NOT NULL,
      source_prefixes TEXT NOT NULL, created REAL NOT NULL,
      PRIMARY KEY(goal_id,path))''',
    '''CREATE TABLE IF NOT EXISTS audit_sample_results (
      goal_id TEXT NOT NULL REFERENCES goals(id), job_id TEXT NOT NULL REFERENCES research_jobs(id),
      area TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES research_runs(id), review_id TEXT,
      verdict TEXT NOT NULL, remaining_gaps TEXT NOT NULL, created REAL NOT NULL,
      PRIMARY KEY(goal_id,job_id))''',
    '''CREATE TABLE IF NOT EXISTS audit_area_updates (
      goal_id TEXT NOT NULL, area TEXT NOT NULL, revision INTEGER NOT NULL,
      status TEXT NOT NULL, priority INTEGER NOT NULL, rationale TEXT NOT NULL,
      next_question TEXT NOT NULL, job_id TEXT REFERENCES research_jobs(id), created REAL NOT NULL,
      PRIMARY KEY(goal_id,area,revision),
      FOREIGN KEY(goal_id,area) REFERENCES audit_areas(goal_id,path))''',
)
COVERAGE_NOTE = ('Sampled means a bounded question received independent evidence review. '
                 'It does not establish complete area or repository coverage; retain remaining questions and gaps.')


def _path(value, *, allow_root=True):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or '\\' in value:
        raise ValueError('Area paths and source prefixes require 1..512 relative POSIX characters')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or (not allow_root and str(path) == '.'):
        raise ValueError('Area paths and source prefixes must stay inside the repository')
    return str(path)


class Audit:
    def __init__(self, store, goal_id):
        self.store, self.db, self.goal_id = store, store.db, goal_id
        if not self.db.execute('SELECT 1 FROM goals WHERE id=?', (goal_id,)).fetchone():
            raise ValueError('Unknown audit goal')
        for statement in SCHEMA:
            self.store.execute(statement)

    def initialize(self, areas):
        """Register immutable inventory metadata; rerunning the same inventory is safe."""
        if not isinstance(areas, list) or not 1 <= len(areas) <= 1000:
            raise ValueError('Audit inventory requires 1..1000 areas')
        prepared = []
        seen = set()
        for area in areas:
            if not isinstance(area, dict):
                raise ValueError('Each audit area must be an object')
            path = _path(area.get('path'))
            if path in seen:
                raise ValueError('Audit area paths must be unique')
            seen.add(path)
            files, size, kind = area.get('files'), area.get('bytes'), area.get('kind')
            if type(files) is not int or type(size) is not int or files < 0 or size < 0:
                raise ValueError('Area files and bytes must be nonnegative integers')
            if not isinstance(kind, str) or not kind.strip() or len(kind) > 80:
                raise ValueError('Area kind requires 1..80 characters')
            prefixes = area.get('source_prefixes', ['sources' if path == '.' else f'sources/{path}'])
            if not isinstance(prefixes, list) or not 1 <= len(prefixes) <= 100:
                raise ValueError('Each area requires 1..100 registered source prefixes')
            prefixes = sorted({_path(p, allow_root=False) for p in prefixes})
            prepared.append((path, files, size, kind, json.dumps(prefixes)))
        from .campaign import transaction
        with transaction(self.db):
            for path, files, size, kind, prefixes in prepared:
                existing = self.db.execute('SELECT files,bytes,kind,source_prefixes FROM audit_areas WHERE goal_id=? AND path=?',
                    (self.goal_id, path)).fetchone()
                if existing:
                    if tuple(existing) != (files, size, kind, prefixes):
                        raise ValueError('Registered audit inventory is immutable; use a new goal for a changed snapshot')
                    continue
                now = time.time()
                self.db.execute('INSERT INTO audit_areas VALUES(?,?,?,?,?,?,?)',
                    (self.goal_id, path, files, size, kind, prefixes, now))
                self.db.execute('INSERT INTO audit_area_updates VALUES(?,?,?,?,?,?,?,?,?)',
                    (self.goal_id, path, 1, 'unexamined', 3, 'Registered repository inventory',
                     'Triage this area and identify its highest-value unresolved question', None, now))
        return self.summary()

    def list(self, status='', offset=0, query=''):
        if status and status not in STATUSES:
            raise ValueError('Use audit status: ' + ', '.join(STATUSES))
        if type(offset) is not int or offset < 0:
            raise ValueError('Audit offset must be nonnegative')
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError('Audit query is limited to 500 characters')
        literal = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        sql = '''FROM audit_areas a JOIN audit_area_updates u ON u.goal_id=a.goal_id AND u.area=a.path
          AND u.revision=(SELECT max(v.revision) FROM audit_area_updates v WHERE v.goal_id=a.goal_id AND v.area=a.path)
          WHERE a.goal_id=? AND (?='' OR u.status=?)
          AND (a.path LIKE ? ESCAPE '\\' OR u.rationale LIKE ? ESCAPE '\\' OR u.next_question LIKE ? ESCAPE '\\')'''
        args = (self.goal_id, status, status, f'%{literal}%', f'%{literal}%', f'%{literal}%')
        total = self.db.execute('SELECT count(*) ' + sql, args).fetchone()[0]
        rows = self.db.execute('''SELECT a.path,a.files,a.bytes,a.kind,a.source_prefixes,u.revision,
            u.status,u.priority,u.rationale,u.next_question,u.job_id,u.created ''' + sql +
            ' ORDER BY u.priority,a.path LIMIT ? OFFSET ?', (*args, PAGE_SIZE, offset)).fetchall()
        records = [dict(row) | {'source_prefixes': json.loads(row['source_prefixes'])} for row in rows]
        return {'areas': records, 'total': total, 'next_offset': offset + len(records) if offset + len(records) < total else None,
                'coverage_note': COVERAGE_NOTE}

    def _reviewed_sample(self, area, job_id):
        if not job_id:
            raise ValueError('Sampled areas require an independently evaluated research job')
        job = self.db.execute('SELECT * FROM research_jobs WHERE id=? AND goal_id=?', (job_id, self.goal_id)).fetchone()
        if not job or job['stage'] != 'research' or job['status'] not in ('supported', 'falsified'):
            raise ValueError('Sample research must belong to this goal and remain independently accepted and current' +
                (f" (status={job['status']})" if job else ''))
        spec = json.loads(job['spec'])
        if spec.get('primary_area') and spec['primary_area'] != area['path']:
            raise ValueError('Sample can count only toward its declared primary_area; other scoped areas remain gaps')
        run = self.db.execute('''SELECT * FROM research_runs WHERE job_id=? AND report IS NOT NULL
            AND finished IS NOT NULL ORDER BY created DESC LIMIT 1''', (job_id,)).fetchone()
        if not run or run['status'] in ('stale', 'interrupted', 'invalidated'):
            raise ValueError('Sampled areas require a completed independent evaluation and research report')
        report, evaluation = json.loads(run['report']), json.loads(run['evaluation']) if run['evaluation'] else None
        revisions = self.db.execute("SELECT revisions FROM research_run_contexts WHERE run_id=? AND role='research'", (run['id'],)).fetchone()
        from .control import Control
        current = Control(self.store).current(self.goal_id)
        if revisions is None or json.loads(revisions['revisions']) != current:
            raise ValueError('Sample research goal, policy or context revisions differ from the current completion contract; retain it as historical evidence')
        # An accepted append-only review may establish the current disposition
        # without rewriting the original process failure or its report.
        review = self.db.execute("SELECT * FROM research_reviews WHERE job_id=? AND run_id=? AND status IN ('supported','falsified') AND finished IS NOT NULL ORDER BY created DESC LIMIT 1", (job_id, run['id'])).fetchone()
        if review:
            if json.loads(review['revisions']) != current:
                raise ValueError('Sample review revisions differ from the current completion contract')
            evaluation = json.loads(review['evaluation']) if review['evaluation'] else None
        if not evaluation or evaluation.get('verdict') not in ('supported', 'falsified') or evaluation['verdict'] != job['status']:
            raise ValueError('Sample requires an accepted independent evaluation; insufficient evidence or process-gate failures remain gaps')
        citations = set(evaluation.get('evidence_ids', []))
        citations.update(report.get('supporting_evidence', []))
        citations.update(report.get('contradictory_evidence', []))
        citations.update(source for finding in report.get('findings', []) for source in finding.get('evidence_ids', []))
        citations.update(source for assessment in evaluation.get('rubric_assessments', []) for source in assessment.get('evidence_ids', []))
        prefixes = json.loads(area['source_prefixes'])
        captured = {row[0] for row in self.db.execute('SELECT source_id FROM research_access WHERE run_id=?', (run['id'],))}
        if not evaluation.get('evidence_ids') or not citations or not citations.issubset(captured):
            raise ValueError('Sample requires current cited evidence captured by its research run')
        sources = [Knowledge(self.store).source(source_id) for source_id in citations]
        if any(source['stale'] for source in sources):
            raise ValueError('Sample requires current cited evidence; at least one source is stale or missing')
        if not any(any(source['locator'] == prefix or source['locator'].startswith(prefix + '/') for prefix in prefixes) for source in sources):
            raise ValueError('Sample requires current cited evidence within this area registered source prefixes')
        return {'job': dict(job), 'run': dict(run), 'report': report, 'evaluation': evaluation,
                'review_id': review['id'] if review else None}

    def record_result(self, job_id):
        """Publish one accepted sample to its primary area, retaining every gap.

        This never marks sibling/parent areas sampled and never completes an
        audit. Rejected and historical results remain useful research history.
        """
        job = self.db.execute('SELECT * FROM research_jobs WHERE id=? AND goal_id=?', (job_id, self.goal_id)).fetchone()
        if not job:
            raise ValueError('Research result must belong to this audit goal')
        primary = json.loads(job['spec']).get('primary_area')
        if not primary:
            return {'recorded': False, 'primary_area': None, 'reason': 'Research has no declared primary_area; no coverage attribution was inferred'}
        area = self.db.execute('SELECT * FROM audit_areas WHERE goal_id=? AND path=?', (self.goal_id, primary)).fetchone()
        if not area:
            return {'recorded': False, 'primary_area': primary, 'reason': 'Primary area is not registered in the audit agenda'}
        try:
            accepted = self._reviewed_sample(area, job_id)
        except (ValueError, OSError) as exc:
            return {'recorded': False, 'primary_area': primary, 'reason': str(exc)}
        if self.db.execute('SELECT 1 FROM audit_sample_results WHERE goal_id=? AND job_id=?', (self.goal_id, job_id)).fetchone():
            return {'recorded': False, 'primary_area': primary, 'reason': 'This accepted sample is already recorded; agenda history was not duplicated'}
        prior = self.db.execute('SELECT priority,next_question FROM audit_area_updates WHERE goal_id=? AND area=? ORDER BY revision DESC LIMIT 1', (self.goal_id, primary)).fetchone()
        report, evaluation = accepted['report'], accepted['evaluation']
        gaps = list(dict.fromkeys(report.get('uncertainties', []) + evaluation.get('workflow_issues', []) +
                                  [evaluation.get('feedback', ''), prior['next_question'],
                                   'One accepted question does not establish complete semantic coverage of this area']))
        gaps = [gap for gap in gaps if gap]
        question = '; '.join(gaps)[:2000]
        rationale = ('Independent ' + evaluation['verdict'] + ' evaluation of one bounded primary-area hypothesis: ' +
                     (json.loads(job['spec']).get('hypothesis') or json.loads(job['spec']).get('question', '')) +
                     '. Remaining questions are retained; this is a sample, not an area completion.')[:4000]
        # A savepoint keeps the sample ledger and agenda update atomic without
        # committing a caller's research-result transaction.
        from .campaign import transaction
        with transaction(self.db):
            self.db.execute('INSERT INTO audit_sample_results VALUES(?,?,?,?,?,?,?,?)',
                (self.goal_id, job_id, primary, accepted['run']['id'], accepted['review_id'], evaluation['verdict'], json.dumps(gaps), time.time()))
            update = self.update(primary, 'sampled', prior['priority'], rationale, question, job_id)
        return {'recorded': True, 'primary_area': primary, 'remaining_gaps': gaps, 'update': update}

    def update(self, area, status, priority, rationale, next_question, job_id=None):
        area = _path(area)
        if status not in STATUSES:
            raise ValueError('Use sampled for bounded evidence review; allowed statuses: ' + ', '.join(STATUSES))
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError('Audit priority must be 1..5; 1 is highest')
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
            raise ValueError('Audit rationale requires 1..4000 characters')
        if not isinstance(next_question, str) or not next_question.strip() or len(next_question) > 2000:
            raise ValueError('Audit next question requires 1..2000 characters')
        registered = self.db.execute('SELECT * FROM audit_areas WHERE goal_id=? AND path=?', (self.goal_id, area)).fetchone()
        if registered is None:
            raise ValueError('Unknown audit area; use the registered inventory')
        if job_id and not self.db.execute('SELECT 1 FROM research_jobs WHERE id=? AND goal_id=?', (job_id, self.goal_id)).fetchone():
            raise ValueError('Research job must belong to this audit goal')
        if status == 'sampled':
            self._reviewed_sample(registered, job_id)
        from .campaign import transaction
        with transaction(self.db):
            revision = self.db.execute('SELECT max(revision)+1 FROM audit_area_updates WHERE goal_id=? AND area=?',
                (self.goal_id, area)).fetchone()[0]
            self.db.execute('INSERT INTO audit_area_updates VALUES(?,?,?,?,?,?,?,?,?)',
                (self.goal_id, area, revision, status, priority, rationale, next_question, job_id, time.time()))
        return {'area': area, 'revision': revision, 'status': status, 'priority': priority,
                'rationale': rationale, 'next_question': next_question, 'job_id': job_id, 'coverage_note': COVERAGE_NOTE}

    def expand(self, area, new_area, directory):
        """Register a focused descendant frontier from actual exported source files."""
        from .navigation import EXCLUDED
        area, new_area, directory = _path(area), _path(new_area), _path(directory)
        if new_area == area or (area != '.' and not new_area.startswith(area + '/')):
            raise ValueError('A new audit subarea must be a logical descendant of its parent')
        parent = self.db.execute('SELECT * FROM audit_areas WHERE goal_id=? AND path=?', (self.goal_id, area)).fetchone()
        if parent is None:
            raise ValueError('Unknown parent audit area')
        root = Path(self.db.execute('SELECT workspace FROM goals WHERE id=?', (self.goal_id,)).fetchone()[0]).resolve()
        candidate = root / directory
        if any(part in EXCLUDED for part in candidate.relative_to(root).parts):
            raise ValueError('Excluded directories cannot become audit subareas')
        if any(path.is_symlink() for path in (candidate, *candidate.parents) if path != root and path.is_relative_to(root)):
            raise ValueError('Audit expansion cannot traverse symlinks')
        source = candidate.resolve()
        if not source.is_relative_to(root) or not source.is_dir():
            raise ValueError('Audit expansion requires an existing directory inside the workspace')
        prefixes = [(root / prefix).resolve() for prefix in json.loads(parent['source_prefixes'])]
        if not any(source != prefix and source.is_relative_to(prefix) for prefix in prefixes):
            raise ValueError('Expansion directory must descend from a registered parent source prefix')
        stems, size, skipped_links, skipped_excluded, entries = set(), 0, 0, 0, 0
        for base, directories, names in os.walk(source, followlinks=False):
            kept = []
            for name in sorted(directories):
                path = Path(base) / name
                if path.is_symlink():
                    skipped_links += 1
                elif name in EXCLUDED:
                    skipped_excluded += 1
                else:
                    kept.append(name)
            directories[:] = kept
            for name in sorted(names):
                path = Path(base) / name
                if path.is_symlink():
                    skipped_links += 1
                    continue
                if name in EXCLUDED:
                    skipped_excluded += 1
                    continue
                if not path.is_file():
                    raise ValueError('Audit source inventory contains a nonregular file')
                entries += 1
                if entries > 200000:
                    raise ValueError('Expansion exceeds 200000 source chunks; choose a narrower directory')
                stems.add(re.sub(r'\.part-\d{4,}$', '', path.relative_to(source).as_posix()))
                size += path.stat().st_size
        if not stems:
            raise ValueError('Expansion directory contains no eligible source files')
        locator = source.relative_to(root).as_posix()
        existing = self.db.execute('SELECT 1 FROM audit_areas WHERE goal_id=? AND path=?', (self.goal_id, new_area)).fetchone()
        self.initialize([{'path': new_area, 'files': len(stems), 'bytes': size,
                          'kind': 'subarea', 'source_prefixes': [locator]}])
        if not existing:
            priority = self.db.execute('SELECT priority FROM audit_area_updates WHERE goal_id=? AND area=? ORDER BY revision DESC LIMIT 1',
                (self.goal_id, area)).fetchone()[0]
            self.update(new_area, 'unexamined', priority,
                f'Expanded from {area}; {len(stems)} unique exported source stems in {entries} chunks, {size} exported bytes. '
                f'Excluded {skipped_links} symlink entries and {skipped_excluded} runtime/dependency entries. Metadata is navigation, not semantic review.',
                'Identify the highest-value bounded contract and its contrary evidence within this subarea')
        return {'area': new_area, 'parent': area, 'source_prefix': locator, 'files': len(stems), 'bytes': size,
                'source_chunks': entries, 'skipped_symlinks': skipped_links, 'skipped_excluded': skipped_excluded,
                'reused': bool(existing), 'summary': self.summary()}

    def completion_gaps(self):
        """Revalidate historical samples without changing immutable agenda history."""
        gaps = []
        rows = self.db.execute("""SELECT a.*,u.status,u.job_id FROM audit_areas a
            JOIN audit_area_updates u ON u.goal_id=a.goal_id AND u.area=a.path
            AND u.revision=(SELECT max(v.revision) FROM audit_area_updates v WHERE v.goal_id=a.goal_id AND v.area=a.path)
            WHERE a.goal_id=? AND a.kind!='subarea' ORDER BY a.path""", (self.goal_id,)).fetchall()
        for row in rows:
            if row['status'] != 'sampled':
                gaps.append({'path': row['path'], 'status': row['status'],
                    'reason': 'No independently evaluated current sample; area remains ' + row['status']})
                continue
            try:
                self._reviewed_sample(row, row['job_id'])
            except (ValueError, OSError) as exc:
                gaps.append({'path': row['path'], 'status': row['status'], 'reason': str(exc)})
        return gaps

    def summary(self):
        counts = {status: 0 for status in STATUSES}
        for row in self.db.execute('''SELECT u.status,count(*) FROM audit_area_updates u WHERE u.goal_id=?
            AND u.revision=(SELECT max(v.revision) FROM audit_area_updates v WHERE v.goal_id=u.goal_id AND v.area=u.area)
            GROUP BY u.status''', (self.goal_id,)):
            counts[row[0]] = row[1]
        totals = self.db.execute("SELECT count(*),coalesce(sum(files),0),coalesce(sum(bytes),0) FROM audit_areas WHERE goal_id=? AND kind!='subarea'",
            (self.goal_id,)).fetchone()
        subareas = self.db.execute("SELECT count(*) FROM audit_areas WHERE goal_id=? AND kind='subarea'", (self.goal_id,)).fetchone()[0]
        return {'areas_total': totals[0] + subareas, 'top_level_areas': totals[0], 'subareas_total': subareas,
                'inventory_files': totals[1], 'inventory_bytes': totals[2],
                'by_status': counts, 'coverage_note': COVERAGE_NOTE}
