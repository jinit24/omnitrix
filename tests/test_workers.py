import asyncio
import json
import sys
import pytest
from omnitrix.workers import CodexWorker
from pydantic import ValidationError


async def fake_process(monkeypatch, script):
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        return await original(sys.executable, '-c', script, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)


async def test_bridge_handoff_and_session(monkeypatch):
    handoff = dict(status='completed', summary='Found metadata', evidence=['pyproject.toml'], next_steps=[])
    script = "import sys,json; payload=json.load(sys.stdin); print(json.dumps({'type':'session','id':'thread-1'}),flush=True); "
    script += 'print(' + repr(json.dumps({'type':'result','handoff':handoff})) + ',flush=True)'
    await fake_process(monkeypatch, script)
    sessions = []
    result = await CodexWorker().run('Inspect metadata', '.', None, sessions.append)
    assert sessions == ['thread-1']
    assert result.summary == 'Found metadata'


async def test_bridge_invalid_handoff_fails(monkeypatch):
    await fake_process(monkeypatch, "import sys;sys.stdin.read();print('{\"type\":\"result\",\"handoff\":{}}')")
    with pytest.raises(ValidationError):
        await CodexWorker().run('Inspect', '.', None, lambda _: None)


async def test_bridge_nonzero_exit_fails(monkeypatch):
    await fake_process(monkeypatch, 'import sys;sys.stdin.read();sys.exit(2)')
    with pytest.raises(RuntimeError, match='exit 2'):
        await CodexWorker().run('Inspect', '.', None, lambda _: None)


async def test_bridge_timeout_cleans_up(monkeypatch):
    await fake_process(monkeypatch, 'import sys,time;sys.stdin.read();time.sleep(20)')
    with pytest.raises(asyncio.TimeoutError):
        await CodexWorker(timeout=.05).run('Inspect', '.', None, lambda _: None)
