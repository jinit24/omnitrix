import sys
import pytest
from scripts.benchmark import score
from omnitrix.implementation import git,AcceptanceCheck


async def test_scoring_uses_candidate_commit_not_dirty_worker_source(tmp_path):
    import subprocess
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    (repo/'service.py').write_text('def process(): return 0\n')
    git(repo,'add','.')
    git(repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','Original candidate')
    candidate=git(repo,'rev-parse','HEAD')
    (repo/'service.py').write_text('def process(): return 1\n')
    checker=tmp_path/'check.py'
    checker.write_text("import os,sys\nsys.path.insert(0,os.environ['OMNITRIX_CANDIDATE'])\nfrom service import process\nassert process()==1\nprint('criterion passed')\n")
    output=tmp_path/'score';output.mkdir()
    checks=[AcceptanceCheck(requirement='Returns one',argv=[sys.executable,str(checker)])]
    results=await score(candidate,repo,checks,output)
    assert not results[0]['passed']
    assert results[0]['candidate']==candidate
    assert 'return 1' in (repo/'service.py').read_text()


async def test_authorized_but_unstarted_candidate_is_scored_without_crashing(tmp_path,monkeypatch):
    import json
    from scripts import benchmark
    task=json.loads((benchmark.ROOT/'benchmarks/tasks.json').read_text())[0]
    original = benchmark.Supervisor.advance
    async def operator_stop_before_coding(self, *args, **kwargs):
        if self.status()['implementations']:
            self.autonomy = 0
            return {'paused': 'Simulated operator stop before the authorized worktree is created'}
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(benchmark.Supervisor, 'advance', operator_stop_before_coding)
    report=await benchmark.run_arm(task,'manager',tmp_path/'arm',False,True,12)
    assert not report['errors']
    assert report['candidate']
    assert report['requirements_satisfied']==1
    assert not report['verified_outcome']


def test_both_research_and_workers_receive_the_same_callback_contracts(tmp_path):
    import json
    from scripts import benchmark
    task=json.loads((benchmark.ROOT/'benchmarks/tasks.json').read_text())[2]
    repo,checks=benchmark.fixture(task,tmp_path)
    documented=(repo/'system.md').read_text()
    assert 'Authoritative acceptance contracts' in documented
    for criterion in task['criteria']:
        assert criterion['code'] in documented
    assert len(checks)==len(task['criteria'])
