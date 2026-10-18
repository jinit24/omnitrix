"""Explicit opt-in: real model usage, separate from deterministic unit tests."""
import os
from pathlib import Path
import pytest
from omnitrix.execution import Execution
from omnitrix.store import Store
from omnitrix.workers import CodexWorker


@pytest.mark.skipif(os.getenv('OMNITRIX_LIVE_CODEX') != '1', reason='Requires live Codex authentication/network')
async def test_codex_start_and_resume(tmp_path):
    workspace = str(Path(__file__).resolve().parents[1])
    store = Store(tmp_path / 'live.db')
    goal = store.create_goal('Inspect metadata', ['Cite project metadata'], workspace)
    execution = Execution(store, CodexWorker(timeout=120))
    task = execution.assign(goal, 'Read pyproject.toml and report the project name with evidence.', 1)
    handoff = await execution.start(task, workspace)
    assert handoff.status == 'completed'
    assert 'omnitrix' in handoff.summary.lower() or any('omnitrix' in e.lower() for e in handoff.evidence)
    session = execution.status(task)['session_id']
    assert session
    followup = await execution.send(task, 'Now report the required Python version from the same file.', workspace)
    assert followup.status == 'completed'
    assert execution.status(task)['session_id'] == session
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 2
