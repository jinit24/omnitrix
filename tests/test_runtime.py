import asyncio
import json
import sys
import pytest
from omnitrix.runtime import CodexRuntime


async def test_native_start_steer_interrupt_and_early_completion(monkeypatch):
    original=asyncio.create_subprocess_exec
    script='''import sys,json
for line in sys.stdin:
 d=json.loads(line)
 if 'id' not in d: continue
 m=d['method'];p=d['params'];r={}
 if m=='thread/start': r={'thread':{'id':'thread'}}
 if m=='turn/start':
  r={'turn':{'id':'turn'}}
  print(json.dumps({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'completed','items':[]}}}),flush=True)
 if m=='turn/steer':
  assert p['expectedTurnId']=='turn' and p['clientUserMessageId']=='message-1'
  r={'turnId':'turn'}
 if m=='thread/read': r={'thread':{'id':'thread','turns':[]}}
 print(json.dumps({'id':d['id'],'result':r}),flush=True)
'''
    async def spawn(*args,**kwargs): return await original(sys.executable,'-c',script,**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    runtime=CodexRuntime()
    threads=[]
    try:
        assert await runtime.start('.', 'Inspect metadata',on_thread=threads.append)==('thread','turn')
        assert threads==['thread']
        assert (await runtime.wait('thread','turn'))['status']=='completed'
        assert (await runtime.steer('thread','turn','new scope','message-1'))['turnId']=='turn'
        await runtime.interrupt('thread','turn')
        assert (await runtime.reconcile('thread'))['thread']['id']=='thread'
    finally:
        await runtime.close()


async def test_execution_probe_is_bounded_cached_and_does_not_relax_sandbox():
    runtime = CodexRuntime()
    calls = []
    async def call(method, params, timeout=30):
        calls.append((method, params, timeout))
        return {'exitCode': 124, 'stdout': '', 'stderr': ''}
    runtime.call = call
    assert (await runtime.probe_execution('/tmp'))['async_thread_bridge'] == 'timed_out'
    await runtime.probe_execution('/tmp')
    assert len(calls) == 1
    assert calls[0][1]['timeoutMs'] == 2000
    assert calls[0][1]['sandboxPolicy'] == {'type': 'workspaceWrite', 'networkAccess': False}


async def test_failed_probe_hands_off_checks_without_waiving_them():
    runtime = CodexRuntime()
    requests = []
    async def connect(): pass
    async def call(method, params, timeout=30):
        requests.append((method, params))
        if method == 'command/exec': return {'exitCode': 124}
        if method == 'thread/start': return {'thread': {'id': 'thread'}}
        if method == 'turn/start': return {'turn': {'id': 'turn'}}
    runtime.connect, runtime.call = connect, call
    await runtime.start('/tmp', 'Repair the contract. All supplied checks are mandatory.', write=True)
    assert requests[1][1]['sandbox'] == 'workspace-write'
    prompt = requests[2][1]['input'][0]['text']
    assert 'independent verifier' in prompt and 'does not waive any acceptance check' in prompt
    assert 'All supplied checks are mandatory' in prompt
