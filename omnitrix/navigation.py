"""Deterministic, bounded workspace navigation for manager source selection."""
from pathlib import Path

EXCLUDED = {'.git', '.omnitrix', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}


def list_resources(workspace, directory='.', offset=0, query=''):
    root = Path(workspace).resolve()
    path = (root / directory).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError('Not a directory inside the workspace')
    if any(part in EXCLUDED for part in path.relative_to(root).parts):
        raise ValueError('Runtime state and dependency directories are excluded')
    if offset < 0 or len(query) > 200:
        raise ValueError('Use nonnegative offset and query at most 200 characters')
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        if child.name in EXCLUDED or child.is_symlink() or query.casefold() not in child.name.casefold():
            continue
        entries.append({'name': child.name, 'directory': child.is_dir()})
    page = entries[offset:offset+40]
    return {'directory': str(path.relative_to(root)), 'entries': page, 'total': len(entries),
            'next_offset': offset+len(page) if offset+len(page) < len(entries) else None}


def find_resources(workspace, query='', offset=0):
    """Find source paths across a wide workspace without reading their contents."""
    import os
    root = Path(workspace).resolve()
    if offset < 0 or offset > 100000 or len(query) > 200:
        raise ValueError('Use offset 0..100000 and a query up to 200 characters')
    matched = []
    for base, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in EXCLUDED and not (Path(base)/d).is_symlink())
        for name in sorted(names):
            path = Path(base)/name
            relative = path.relative_to(root).as_posix()
            if not path.is_symlink() and query.casefold() in relative.casefold():
                matched.append(relative)
    matched.sort()
    return {'paths': matched[offset:offset+40], 'total':len(matched),
            'next_offset':offset+40 if offset+40<len(matched) else None}


def search_workspace(workspace, query, directory='sources', offset=0):
    """Bounded literal search. Returned matches guide scope selection, not citations."""
    import subprocess
    root = Path(workspace).resolve()
    path = (root/directory).resolve()
    if not path.is_relative_to(root) or not path.is_dir() or any(p in EXCLUDED for p in path.relative_to(root).parts):
        raise ValueError('Search directory must be inside the source workspace')
    if not query.strip() or len(query)>200 or offset<0 or offset>500:
        raise ValueError('Use query length 1..200 and offset 0..500; narrow the directory for more matches')
    # rg bounds per-file matches and line lengths; stop after a bounded output
    # page so a common pattern cannot fill memory or the model context.
    args=['rg','--json','--sort','path','-i','-F','--max-count','3','--max-columns','400','--glob','!*.sqlite*']
    for excluded in sorted(EXCLUDED):
        args += ['--glob','!'+excluded+'/**']
    args += ['--',query,str(path)]
    process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    matches=[]
    import json, threading
    timer=threading.Timer(10,process.kill)
    timer.start()
    try:
        for line in process.stdout:
            item=json.loads(line)
            if item.get('type')!='match': continue
            data=item['data'];relative=Path(data['path']['text']).relative_to(root).as_posix()
            matches.append({'locator':relative,'line':data['line_number'],'text':data['lines']['text'].strip()[:400]})
            if len(matches)>=offset+21: break
    finally:
        if process.poll() is None: process.kill()
        process.wait();process.stdout.close();timer.cancel()
    return {'matches':matches[offset:offset+20],
            'next_offset':offset+20 if len(matches)>offset+20 else None,
            'bounded':True,'note':'Navigation only. Scope a research read for citable evidence. Results may stop at time or per-file match limits.'}
