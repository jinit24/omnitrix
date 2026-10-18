import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import pytest
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.supervisor import Supervisor
from test_interactive import Runtime,long_worker


async def test_sigkill_manager_preserves_ownership_messages_and_conversation(tmp_path):
    path=tmp_path/'state.db'
    store=Store(path)
    goal=store.create_goal('Long running task',['Preserve evidence'],tmp_path)
    code='''import os,signal,sys,time,json
from omnitrix.store import Store
from omnitrix.control import Control
from omnitrix.execution import Execution
s=Store(sys.argv[1]);c=Control(s);c.initialize(sys.argv[2])
t=Execution(s,type('Adapter',(),{'name':'mock'})()).assign(sys.argv[2],'Bounded job',1)
s.execute("INSERT INTO task_runtime(task_id,goal_revision,policy_revision,state,spec,owner,session_id,turn_id,started,last_progress,deadline) VALUES(?,1,1,'running','{}','old-owner','thread','turn',?,?,?)",(t,time.time(),time.time(),time.time()+100))
c.message(sys.argv[2],'steering',{'instruction':'Change approach'},t,'manager_to_worker',message_id='lost-response',state='sending')
c.event(sys.argv[2],'evidence_progress',{'artifact':'retained'},t)
os.kill(os.getpid(),signal.SIGKILL)
'''
    proc=subprocess.run([sys.executable,'-c',code,str(path),goal],capture_output=True)
    assert proc.returncode==-signal.SIGKILL
    fresh=Store(path)
    supervisor=await Supervisor(fresh,goal,runtime=Runtime()).open()
    assert fresh.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='uncertain'
    assert fresh.db.execute('SELECT state FROM messages WHERE id=?',('lost-response',)).fetchone()[0]=='uncertain'
    assert fresh.db.execute("SELECT 1 FROM activity WHERE kind='evidence_progress'").fetchone()
    task=fresh.db.execute('SELECT task_id FROM task_runtime').fetchone()[0]
    assert (await supervisor.advance(steps=2))['blocked']
    await supervisor.reconcile(task)
    assert fresh.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='reconciled'
    supervisor.resolve_delivery('lost-response',True,'Observed matching instruction in saved native thread history')
    assert fresh.db.execute('SELECT state FROM messages').fetchone()[0]=='acknowledged'
    await supervisor.close()


async def test_tokens_and_duplicate_activity_are_not_meaningful_progress(tmp_path):
    store=Store(tmp_path/'state.db');goal=store.create_goal('Improve job',['Observe outcomes'],tmp_path)
    supervisor=await Supervisor(store,goal,runtime=Runtime()).open()
    started,release=asyncio.Event(),asyncio.Event()
    task=await long_worker(supervisor,started,release)
    await started.wait()
    before=store.db.execute('SELECT last_progress FROM task_runtime').fetchone()[0]
    await supervisor.on_runtime_event({'method':'thread/tokenUsage/updated','params':{'threadId':'thread','turnId':'turn','tokenUsage':{'total':{'totalTokens':1000}}}})
    assert store.db.execute('SELECT last_progress FROM task_runtime').fetchone()[0]==before
    for index in range(2):
        await supervisor.on_runtime_event({'method':'item/completed','params':{'threadId':'thread','turnId':'turn',
            'item':{'id':str(index),'type':'commandExecution','command':'pytest','aggregatedOutput':'1 passed','exitCode':0}}})
    assert store.db.execute('SELECT progress_count FROM task_runtime').fetchone()[0]==1
    await supervisor.monitor(now=time.time()+400)
    assert store.db.execute('SELECT state FROM task_runtime').fetchone()[0]=='paused'
    await supervisor.close()
