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
