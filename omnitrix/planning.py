"""Deterministic scope and read-feasibility checks, not a semantic correctness claim."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from .knowledge import Knowledge, read_source_bytes
from .research_models import ResearchPlan

EXCLUDED = {'.git', '.omnitrix', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}


def safe_locator(workspace, locator, directory=False):
    root = Path(workspace).resolve()
    if not isinstance(locator, str) or not locator or len(locator) > 512:
        raise ValueError('A bounded relative source locator is required')
    relative = PurePosixPath(locator)
    if relative.is_absolute() or '..' in relative.parts or '\\' in locator or any(p in EXCLUDED for p in relative.parts):
        raise ValueError('Source locators must stay inside approved workspace roots')
    candidate = root / str(relative)
    if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root and p.is_relative_to(root)):
        raise ValueError('Research scope cannot traverse symlinks')
    path = candidate.resolve()
    if not path.is_relative_to(root) or (not path.is_dir() if directory else not path.is_file()):
        raise ValueError('Scope requires an existing regular source file or approved directory')
    return path.relative_to(root).as_posix()


def source_info(workspace, locator):
    canonical = safe_locator(workspace, locator)
    _, data = read_source_bytes(workspace, canonical)
    return canonical, data.decode('utf-8').splitlines(), hashlib.sha256(data).hexdigest()


def evidence_lines(source, lines):
    """Count only complete lines actually present in the captured excerpt."""
    found = set()
    for text in source['excerpt'].splitlines():
        match = re.match(r'^(\d+): (.*)$', text)
        if match:
            number = int(match[1])
            if 1 <= number <= len(lines) and match[2] == lines[number - 1]:
                found.add(number)
    return found


def read_windows(positions, lines=None):
    count, through = 0, 0
    for line in sorted(positions):
        if line > through:
            count += 1
            through = line + 99
            if lines is not None:
                used, through = 0, line - 1
                for number in range(line, min(len(lines), line + 99) + 1):
                    width = len(f'{number}: {lines[number - 1]}') + int(number != line)
                    if used + width > 12000:
                        break
                    used += width
                    through = number
                if through < line:
                    raise ValueError('Required source line exceeds the bounded excerpt size; provide a smaller export')
    return count


def preflight_plan(store, goal_id, stage, spec, require_checkpoint=False):
    spec = ResearchPlan.model_validate(spec)
    if stage not in ('research', 'discovery') or (stage == 'research') != bool(spec.hypothesis):
        raise ValueError('Focused research requires a hypothesis; discovery must not supply one')
    goal = store.db.execute('SELECT workspace FROM goals WHERE id=?', (goal_id,)).fetchone()
    if goal is None:
        raise ValueError('Unknown goal')
    workspace = goal['workspace']
    info = {canonical: (lines, revision) for canonical, lines, revision in
            (source_info(workspace, source) for source in spec.scope)}
    scope = sorted(info)
    roots = sorted({safe_locator(workspace, root, directory=True) for root in spec.dependency_roots})
    if spec.allow_scope_expansion and not roots:
        raise ValueError('Scope expansion requires explicit approved dependency_roots')
    if any(dependency.required for dependency in spec.unresolved_dependencies):
        missing = ', '.join(d.symbol for d in spec.unresolved_dependencies if d.required)
        raise ValueError('Unresolved required dependencies must be scoped before dispatch: ' + missing)
    if spec.primary_area:
        exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_areas'").fetchone()
        area = store.db.execute('SELECT source_prefixes FROM audit_areas WHERE goal_id=? AND path=?', (goal_id, spec.primary_area)).fetchone() if exists else None
        if area is None:
            raise ValueError('primary_area must name a registered audit area in this goal')
        prefixes = json.loads(area['source_prefixes'])
        if not any(source == prefix or source.startswith(prefix + '/') for source in scope for prefix in prefixes):
            raise ValueError('Plan scope must include a source within primary_area')
    pending, normalized, knowledge = {}, [], Knowledge(store)
    for obligation in spec.evidence_obligations:
        locator = safe_locator(workspace, obligation.locator)
        if locator not in info:
            if obligation.required:
                raise ValueError('Required evidence obligation is outside scope: ' + locator)
            normalized.append(obligation.model_copy(update={'locator': locator}))
            continue
        lines, _ = info[locator]
        end = obligation.end_line if obligation.end_line is not None else len(lines)
        if not 1 <= obligation.start_line <= end <= len(lines):
            raise ValueError('Evidence obligation range exceeds current source: ' + locator)
        required = set(range(obligation.start_line, end + 1))
        if obligation.status == 'dependency' and obligation.required:
            raise ValueError('Required dependency obligation is unresolved: ' + locator)
        observed = set()
        for evidence_id in obligation.evidence_ids:
            source = knowledge.source(evidence_id)
            if source['workspace'] != workspace or source['locator'] != locator or source['stale']:
                raise ValueError('Captured obligation cites stale or unrelated evidence')
            observed.update(evidence_lines(source, lines))
        if obligation.status == 'captured' and not required.issubset(observed):
            raise ValueError('Captured obligation lacks complete current evidence for its range')
        if obligation.required:
            pending.setdefault(locator, set()).update(required - observed)
        normalized.append(obligation.model_copy(update={'locator': locator, 'end_line': end}))
    minimum = sum(read_windows(positions, info[locator][0]) for locator, positions in pending.items())
    if require_checkpoint and not normalized:
        # A checkpoint must cite at least one observation, even for a legacy plan.
        minimum = max(1, minimum)
    reserve = int(require_checkpoint)
    if minimum + reserve > spec.budget.tool_calls:
        raise ValueError(f'Infeasible research tool budget: needs at least {minimum} source reads + {reserve} reserved checkpoint call(s), has {spec.budget.tool_calls}')
    normalized_spec = spec.model_copy(update={'scope': scope, 'dependency_roots': roots, 'evidence_obligations': normalized})
    proof = {'minimum_read_calls': minimum, 'reserved_checkpoint_calls': reserve,
        'minimum_tool_calls': minimum + reserve, 'file_revisions': {source: value[1] for source, value in info.items()},
        'obligations': [obligation.model_dump() for obligation in normalized],
        'review_budget_usd': spec.review_budget_usd,
        'review_budget_note': 'Explicit protected envelope; actual request reservations must fit and can still block evaluation',
        'legacy_scope_only': not bool(normalized), 'scope_closure': 'Declared obligations checked; semantic completeness is not inferred'}
    return {'spec': normalized_spec, 'proof': proof}
