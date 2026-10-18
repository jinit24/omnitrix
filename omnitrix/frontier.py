"""A disk-backed index and unexplored frontier for large Git repositories."""
import codecs
from pathlib import Path
import subprocess

from .knowledge import source_path
from .resources import resource_kind

SCHEMA = '''
CREATE TABLE IF NOT EXISTS frontier_meta (
 goal_id TEXT PRIMARY KEY REFERENCES goals(id), head TEXT NOT NULL,
 complete INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS frontier_files (
 goal_id TEXT NOT NULL REFERENCES goals(id), locator TEXT NOT NULL, kind TEXT NOT NULL,
 bucket TEXT NOT NULL, size_bytes INTEGER NOT NULL, origin TEXT NOT NULL DEFAULT 'git', PRIMARY KEY(goal_id,locator));
CREATE INDEX IF NOT EXISTS frontier_kind ON frontier_files(goal_id,kind,locator);
CREATE INDEX IF NOT EXISTS frontier_bucket ON frontier_files(goal_id,bucket,locator);
'''


class Frontier:
    def __init__(self, store, goal_id):
        self.store, self.goal_id = store, goal_id
        row = store.db.execute('SELECT workspace FROM goals WHERE id=?', (goal_id,)).fetchone()
        if not row:
            raise ValueError('Unknown goal')
        self.workspace = Path(row[0])

    def build(self):
        head = subprocess.check_output(['git', '-C', str(self.workspace), 'rev-parse', 'HEAD'], text=True).strip()
        prior = self.store.db.execute('SELECT * FROM frontier_meta WHERE goal_id=?', (self.goal_id,)).fetchone()
        if prior and prior['head'] != head:
            raise ValueError('Indexed checkout HEAD changed; retain the pinned checkout or use a separate goal')
        if prior and prior['complete']:
            return self.summary()
        self.store.execute('INSERT OR IGNORE INTO frontier_meta(goal_id,head) VALUES(?,?)', (self.goal_id, head))
        # Stream Git's index rather than recursively materializing the whole tree.
        proc = subprocess.Popen(['git', '-C', str(self.workspace), 'ls-files', '-z'], stdout=subprocess.PIPE)
        buffer = b''
        batch, skipped = [], 0
        try:
            while chunk := proc.stdout.read(65536):
                parts = (buffer + chunk).split(b'\0')
                buffer = parts.pop()
                for raw in parts:
                    locator = raw.decode('utf-8')
                    try:
                        path = source_path(self.workspace, locator)
                        if (self.workspace / locator).is_symlink():
                            raise ValueError('Symlink')
                        with path.open('rb') as stream:
                            sample = stream.read(4096)
                        if b'\0' in sample:
                            raise ValueError('Binary source')
                        codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
                    except (OSError, ValueError, UnicodeError):
                        skipped += 1
                        continue
                    size = path.stat().st_size
                    if size > 32 * 1024 * 1024:
                        skipped += 1
                        continue
                    parts_path = Path(locator).parts
                    bucket = '/'.join(parts_path[:2]) if len(parts_path) > 2 else parts_path[0] if len(parts_path) > 1 else '.'
                    batch.append((self.goal_id, locator, resource_kind(locator), bucket, size))
                    if len(batch) >= 500:
                        self._save(batch)
                        batch = []
            self._save(batch)
            if proc.wait() != 0 or buffer:
                raise RuntimeError('Git inventory did not complete; retained partial index is not whole-repository coverage')
            self.store.execute('UPDATE frontier_meta SET complete=1,skipped=? WHERE goal_id=?', (skipped, self.goal_id))
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return self.summary()

    def _save(self, rows):
        with self.store.db:
            self.store.db.executemany('INSERT OR REPLACE INTO frontier_files(goal_id,locator,kind,bucket,size_bytes) VALUES(?,?,?,?,?)', rows)

    def register(self, locators):
        """Explicitly index local exported logs/measurements without editing source."""
        rows=[]
        for locator in locators:
            path=source_path(self.workspace,locator)
            if (self.workspace/locator).is_symlink() or path.stat().st_size>32*1024*1024:
                raise ValueError('Evidence must be a regular UTF-8 file no larger than 32 MiB')
            # Validate the complete exported text, not just the inventory sample.
            with path.open(encoding='utf-8') as stream:
                while chunk:=stream.read(65536):
                    if '\0' in chunk:raise ValueError('Binary evidence is unsupported')
            relative=str(path.relative_to(self.workspace))
            bucket='/'.join(Path(relative).parts[:2]) if len(Path(relative).parts)>2 else '.'
            rows.append((self.goal_id,relative,resource_kind(relative),bucket,path.stat().st_size))
        with self.store.db:
            for row in rows:
                self.store.db.execute("INSERT INTO frontier_files(goal_id,locator,kind,bucket,size_bytes,origin) VALUES(?,?,?,?,?,'export') ON CONFLICT(goal_id,locator) DO UPDATE SET size_bytes=excluded.size_bytes",row)
        from .control import Control
        Control(self.store).event(self.goal_id,'evidence_registered',{'locators':[r[1] for r in rows],
            'note':'Local exports, not Git-tracked files or evidence of production impact; inspect workload/window metadata.'})
        return self.summary()

    def summary(self):
        meta = self.store.db.execute('SELECT * FROM frontier_meta WHERE goal_id=?', (self.goal_id,)).fetchone()
        if not meta:
            return None
        counts = {r['kind']: r['n'] for r in self.store.db.execute('SELECT kind,count(*) n FROM frontier_files WHERE goal_id=? GROUP BY kind', (self.goal_id,))}
        origins = {r['origin']:r['n'] for r in self.store.db.execute('SELECT origin,count(*) n FROM frontier_files WHERE goal_id=? GROUP BY origin',(self.goal_id,))}
        seen = self.store.db.execute('''SELECT count(DISTINCT f.locator) FROM frontier_files f
            JOIN research_sources s ON s.locator=f.locator AND s.workspace=?
            JOIN research_access a ON a.source_id=s.id JOIN research_runs r ON r.id=a.run_id
            JOIN research_jobs j ON j.id=r.job_id AND j.goal_id=f.goal_id WHERE f.goal_id=?''',
            (str(self.workspace), self.goal_id)).fetchone()[0]
        return dict(meta) | {'files': sum(counts.values()), 'by_kind': counts, 'by_origin':origins,'observed_files': seen,
                            'basis': 'Git-tracked UTF-8 files and explicitly registered local exports <=32 MiB; binaries, unavailable files and oversized files counted as skipped. Excerpts do not establish full coverage.'}

    def page(self, query='', kind=None, after='', limit=20, prefix=''):
        limit = min(max(limit, 1), 40)
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        prefix = '' if prefix in ('','.') else prefix.rstrip('/')+'/'
        escaped_prefix = prefix.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        rows = self.store.db.execute('''SELECT locator,kind,bucket,size_bytes,origin FROM frontier_files
            WHERE goal_id=? AND locator>? AND locator LIKE ? ESCAPE '\\' AND (? IS NULL OR kind=?)
            AND locator LIKE ? ESCAPE '\\' ORDER BY locator LIMIT ?''', (self.goal_id, after, '%'+escaped+'%', kind, kind, escaped_prefix+'%',limit+1)).fetchall()
        components=self.store.db.execute('''SELECT bucket,count(*) AS matches,min(locator) AS example_locator FROM frontier_files
            WHERE goal_id=? AND locator LIKE ? ESCAPE '\\' AND (? IS NULL OR kind=?)
            AND locator LIKE ? ESCAPE '\\' GROUP BY bucket ORDER BY bucket''',
            (self.goal_id,'%'+escaped+'%',kind,kind,escaped_prefix+'%')).fetchall()
        return {'entries': [dict(r) for r in rows[:limit]], 'next_after': rows[limit-1]['locator'] if len(rows)>limit else None,
                'query': query, 'kind': kind, 'directory':prefix,
                'total_matches':sum(row['matches'] for row in components),
                'matched_components':[dict(row) for row in components[:8]],'components_truncated':len(components)>8,
                'query_scope':'Literal pathname substring only, never file contents. directory must name a directory. Follow next_after before claiming absence; component samples may reveal paths beyond this page. To inspect contents, return a research plan with real file paths; researchers have read_source/search_source.'}

    def suggestions(self, limit=12):
        rows = self.store.db.execute('''WITH seen AS (
            SELECT DISTINCT s.locator FROM research_sources s JOIN research_access a ON a.source_id=s.id
            JOIN research_runs r ON r.id=a.run_id JOIN research_jobs j ON j.id=r.job_id
            WHERE j.goal_id=?), candidates AS (
            SELECT f.*,row_number() OVER (PARTITION BY bucket ORDER BY
                CASE kind WHEN 'metrics' THEN 0 WHEN 'logs' THEN 0 WHEN 'code' THEN 1 ELSE 2 END,locator) AS branch_rank
            FROM frontier_files f WHERE goal_id=? AND NOT EXISTS(SELECT 1 FROM seen WHERE seen.locator=f.locator))
            SELECT locator,kind,bucket,size_bytes,origin FROM candidates ORDER BY CASE origin WHEN 'export' THEN 0 ELSE 1 END,branch_rank,bucket,locator LIMIT ?''',
            (self.goal_id, self.goal_id, min(max(limit,1),40))).fetchall()
        return [dict(r) for r in rows]
